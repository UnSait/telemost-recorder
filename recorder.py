"""Запись встречи Яндекс.Телемост через Playwright с WebRTC-захватом звука."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from audio_extractor import AudioExtractionError, extract_audio
from dom_scanner import (
    DomScannerError,
    MeetingEndedError,
    assert_meeting_not_ended,
    detect_meeting_ended,
    fill_name_and_join,
    is_csat_feedback_visible,
    is_telemost_lobby,
)
from webrtc_audio import (
    CHUNK_FLUSH_INTERVAL_SEC,
    ensure_audio_capture,
    finalize_webrtc_audio,
    flush_audio_chunks_to_file,
    get_capture_status,
    install_audio_capture,
)

logger = logging.getLogger(__name__)

MEETING_END_PATTERN = re.compile(
    r"встреча завершена|meeting ended|встречу завершил|встреча окончена",
    re.IGNORECASE,
)
AUTH_PATTERN = re.compile(
    r"логин|login|email|пароль|password|войти в аккаунт|sign in|авториз",
    re.IGNORECASE,
)
TIMER_PATTERN = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


def _use_headed_browser(debug: bool) -> bool:
    """
    Headed-режим только при наличии X-сервера ($DISPLAY).

    На headless-сервере/Docker --debug включает скриншоты и лог кандидатов,
    но браузер остаётся headless — иначе Chromium падает без X11.
    """
    return debug and bool(os.environ.get("DISPLAY"))


@dataclass
class RecorderConfig:
    """Конфигурация записи встречи."""

    meeting_url: str
    bot_name: str
    output_dir: Path
    max_duration: int
    audio_format: str
    debug: bool
    output_filename: str
    on_status: Callable[[str], None] | None = None


class TelemostRecorder:
    """Асинхронный рекордер встреч Телемост через Playwright."""

    def __init__(
        self,
        config: RecorderConfig,
        shutdown_event: asyncio.Event | None = None,
    ) -> None:
        self._config = config
        self._shutdown_event = shutdown_event or asyncio.Event()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._temp_dir: Path | None = None
        self._debug_dir: Path | None = None
        self._step_counter = 0
        self._recording_started = False
        self._stop_reason = "normal"
        self._page_closed_event = asyncio.Event()
        self._meeting_id = self._extract_meeting_id(config.meeting_url)
        self._finalize_done = False
        self._finalize_result: Path | None = None
        self._webrtc_saved_path: Path | None = None
        self._webrtc_webm_path: Path | None = None
        self._chunk_flush_task: asyncio.Task[None] | None = None
        self._bytes_flushed = 0

    @staticmethod
    def _extract_meeting_id(meeting_url: str) -> str:
        """Идентификатор встречи из URL для отслеживания редиректов."""
        path = urlparse(meeting_url).path.rstrip("/")
        return path.split("/")[-1] if path else ""

    def _is_still_on_meeting_url(self) -> bool:
        """Проверяет, что вкладка всё ещё на странице встречи."""
        if not self._page or not self._meeting_id:
            return True
        return self._meeting_id in self._page.url

    def _status(self, message: str) -> None:
        """Передаёт статусное сообщение в callback (emoji-stdout)."""
        if self._config.on_status:
            self._config.on_status(message)

    async def _debug_screenshot(self, name: str) -> None:
        """Сохраняет скриншот в debug-режиме."""
        if not self._config.debug or not self._page or not self._debug_dir:
            return
        self._step_counter += 1
        path = self._debug_dir / f"step_{self._step_counter:02d}_{name}.png"
        try:
            await self._page.screenshot(path=str(path), full_page=True)
            logger.debug("Скриншот: %s", path)
        except Exception as exc:
            logger.warning("Не удалось сохранить скриншот %s: %s", path, exc)

    async def _save_auth_artifacts(self) -> tuple[Path, Path]:
        """Сохраняет скриншот и HTML при обнаружении формы авторизации."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        artifact_dir = self._config.output_dir / f"debug_{ts}"
        artifact_dir.mkdir(parents=True, exist_ok=True)

        screenshot_path = artifact_dir / f"auth_required_{ts}.png"
        html_path = artifact_dir / f"auth_required_{ts}.html"

        if self._page:
            await self._page.screenshot(path=str(screenshot_path), full_page=True)
            html_path.write_text(await self._page.content(), encoding="utf-8")

        return screenshot_path, html_path

    async def _detect_auth_required(self) -> bool:
        """
        Определяет, требует ли встреча авторизации (не анонимный вход).

        Семантические признаки: поля логина/пароля без поля имени гостя.
        """
        if not self._page:
            return False

        has_auth_field = False
        textboxes = self._page.get_by_role("textbox")
        count = await textboxes.count()

        for i in range(count):
            try:
                box = textboxes.nth(i)
                if not await box.is_visible():
                    continue
                attrs = await box.evaluate(
                    """el => ({
                        ariaLabel: el.getAttribute('aria-label') || '',
                        placeholder: el.getAttribute('placeholder') || '',
                        type: el.getAttribute('type') || '',
                        name: el.getAttribute('name') || '',
                    })"""
                )
                searchable = " ".join(
                    filter(
                        None,
                        [
                            attrs.get("ariaLabel", ""),
                            attrs.get("placeholder", ""),
                            attrs.get("type", ""),
                            attrs.get("name", ""),
                        ],
                    )
                )
                if AUTH_PATTERN.search(searchable):
                    has_auth_field = True
                    break
            except Exception:
                continue

        # Кнопка «Войти» / «Sign in» без гостевого входа
        sign_in_buttons = self._page.get_by_role("button").filter(
            has_text=re.compile(r"войти|sign in|log in|авториз", re.IGNORECASE)
        )
        has_sign_in = await sign_in_buttons.count() > 0

        # Поле пароля — явный признак авторизации
        password_fields = self._page.locator('input[type="password"]')
        has_password = await password_fields.count() > 0

        if has_password or (has_auth_field and has_sign_in):
            return True

        return False

    async def _is_timer_visible(self) -> bool:
        """Проверяет наличие видимого таймера встречи на странице."""
        if not self._page:
            return False

        elements = self._page.get_by_text(TIMER_PATTERN)
        count = await elements.count()
        for i in range(count):
            try:
                el = elements.nth(i)
                if await el.is_visible():
                    text = (await el.inner_text()).strip()
                    if TIMER_PATTERN.match(text):
                        return True
            except Exception:
                continue
        return False

    async def _wait_for_meeting_end(self) -> str:
        """
        Ожидает окончания встречи по нескольким сигналам параллельно.

        Returns:
            Причина завершения: normal, max_duration, page_closed, signal.
        """
        assert self._page is not None

        page = self._page
        timer_was_visible = False
        timer_visible_since: float | None = None

        async def wait_end_text() -> str:
            while True:
                if self._shutdown_event.is_set():
                    return "signal"
                try:
                    end_el = page.get_by_text(MEETING_END_PATTERN)
                    if await end_el.count() > 0:
                        for i in range(await end_el.count()):
                            if await end_el.nth(i).is_visible():
                                await self._on_meeting_end_detected()
                                return "meeting_ended"
                except Exception:
                    pass
                await asyncio.sleep(2)

        async def wait_meeting_ended_ui() -> str:
            """CSAT, редирект на главную или текст о завершении встречи."""
            while True:
                if self._shutdown_event.is_set():
                    return "signal"
                if await is_csat_feedback_visible(page):
                    logger.info("Встреча завершена: показан опрос CSAT")
                    await self._on_meeting_end_detected()
                    return "meeting_ended"
                reason = await detect_meeting_ended(page)
                if reason:
                    logger.info("Встреча завершена: %s", reason)
                    await self._on_meeting_end_detected()
                    return "meeting_ended"
                if not self._is_still_on_meeting_url():
                    if await is_telemost_lobby(page) or await is_csat_feedback_visible(page):
                        logger.info("Редирект с URL встречи после завершения")
                        await self._on_meeting_end_detected()
                        return "meeting_ended"
                await asyncio.sleep(2)

        async def wait_timer_disappear() -> str:
            nonlocal timer_was_visible, timer_visible_since
            while True:
                if self._shutdown_event.is_set():
                    return "signal"
                visible = await self._is_timer_visible()
                now = time.monotonic()

                if visible:
                    if not timer_was_visible:
                        timer_was_visible = True
                        timer_visible_since = now
                elif timer_was_visible and timer_visible_since is not None:
                    # Таймер был виден 30+ секунд и исчез — встреча завершена
                    if now - timer_visible_since >= 30:
                        await self._on_meeting_end_detected()
                        return "meeting_ended"

                await asyncio.sleep(2)

        async def wait_page_close() -> str:
            await self._page_closed_event.wait()
            return "page_closed"

        async def wait_max_duration() -> str:
            await asyncio.sleep(self._config.max_duration)
            return "max_duration"

        async def wait_shutdown() -> str:
            await self._shutdown_event.wait()
            return "signal"

        tasks = [
            asyncio.create_task(wait_end_text(), name="end_text"),
            asyncio.create_task(wait_meeting_ended_ui(), name="ended_ui"),
            asyncio.create_task(wait_timer_disappear(), name="timer"),
            asyncio.create_task(wait_page_close(), name="page_close"),
            asyncio.create_task(wait_max_duration(), name="max_duration"),
            asyncio.create_task(wait_shutdown(), name="shutdown"),
        ]

        try:
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            reason = done.pop().result()
            return reason
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _cancel_chunk_flush(self) -> None:
        """Останавливает фоновый сброс чанков на диск."""
        if self._chunk_flush_task and not self._chunk_flush_task.done():
            self._chunk_flush_task.cancel()
            try:
                await self._chunk_flush_task
            except asyncio.CancelledError:
                pass
        self._chunk_flush_task = None

    async def _periodic_chunk_flush(self) -> None:
        """Каждые N секунд сбрасывает WebRTC-чанки из RAM браузера на диск."""
        try:
            while not self._shutdown_event.is_set():
                await asyncio.sleep(CHUNK_FLUSH_INTERVAL_SEC)
                if self._webrtc_saved_path or not self._page or not self._webrtc_webm_path:
                    continue
                nbytes = await flush_audio_chunks_to_file(self._page, self._webrtc_webm_path)
                if nbytes:
                    self._bytes_flushed += nbytes
        except asyncio.CancelledError:
            pass

    async def _flush_webrtc_audio(self) -> Path | None:
        """Срочно останавливает запись и сохраняет WebRTC-аудио до навигации на CSAT/лобби."""
        if self._webrtc_saved_path or not self._page or not self._webrtc_webm_path:
            return self._webrtc_saved_path

        try:
            await self._cancel_chunk_flush()
            saved = await finalize_webrtc_audio(self._page, self._webrtc_webm_path)
            if saved:
                self._webrtc_saved_path = saved
                logger.info("WebRTC-аудио сохранено досрочно: %s", saved)
        except Exception as exc:
            logger.warning("Досрочное сохранение WebRTC-аудио не удалось: %s", exc)
        return self._webrtc_saved_path

    async def _on_meeting_end_detected(self) -> None:
        """Вызывается сразу при детекции конца встречи — до смены страницы."""
        await self._flush_webrtc_audio()

    def _cleanup_temp_dir(self) -> None:
        """Удаляет временную директорию с WebRTC WebM."""
        if not self._temp_dir or not self._temp_dir.exists():
            return
        try:
            shutil.rmtree(self._temp_dir)
            logger.debug("Временная директория удалена: %s", self._temp_dir)
        except OSError as exc:
            logger.warning("Не удалось удалить временную директорию %s: %s", self._temp_dir, exc)
        finally:
            self._temp_dir = None

    async def _finalize_recording(self, reason: str) -> Path | None:
        """Останавливает WebRTC-запись, закрывает браузер и сохраняет аудио."""
        if self._finalize_done:
            return self._finalize_result

        await self._cancel_chunk_flush()

        output_path = self._config.output_dir / self._config.output_filename
        webrtc_webm: Path | None = self._webrtc_saved_path

        if not webrtc_webm and self._page and self._webrtc_webm_path:
            self._status("🎵 Сохранение аудиозаписи...")
            try:
                saved = await finalize_webrtc_audio(self._page, self._webrtc_webm_path)
                if saved:
                    logger.info("WebRTC WebM сохранён: %s", saved)
                    self._webrtc_saved_path = saved
                    webrtc_webm = saved
                else:
                    status = await get_capture_status(self._page)
                    logger.warning(
                        "WebRTC-аудио не захвачено (tracks=%s, recorder=%s, frames=%s, flushed=%d)",
                        status.get("trackCount"),
                        status.get("recorderState"),
                        status.get("frame", "?"),
                        self._bytes_flushed,
                    )
            except Exception as exc:
                logger.warning("Ошибка сохранения WebRTC-аудио: %s", exc)
        elif webrtc_webm:
            logger.info("Используем досрочно сохранённое WebRTC-аудио: %s", webrtc_webm)

        await self._close_browser()

        try:
            self._status("🎵 Извлечение аудио...")

            if not webrtc_webm or not webrtc_webm.exists() or webrtc_webm.stat().st_size == 0:
                logger.error("Аудиозапись не найдена (причина: %s)", reason)
                self._finalize_done = True
                raise AudioExtractionError(
                    "WebRTC не захватил звук встречи (tracks=0 или пустой файл). "
                    "Запустите с --debug и проверьте строку «Аудиозахват: tracks=…»."
                )

            audio_path = await extract_audio(
                webrtc_webm,
                output_path,
                self._config.audio_format,  # type: ignore[arg-type]
            )
            self._finalize_done = True
            self._finalize_result = audio_path
            return audio_path
        finally:
            self._cleanup_temp_dir()

    async def _close_browser(self) -> None:
        """Корректно закрывает браузер и Playwright."""
        try:
            if self._context:
                await self._context.close()
        except Exception as exc:
            logger.warning("Ошибка закрытия контекста: %s", exc)
        finally:
            self._context = None
            self._page = None

        try:
            if self._browser:
                await self._browser.close()
        except Exception as exc:
            logger.warning("Ошибка закрытия браузера: %s", exc)
        finally:
            self._browser = None

        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception as exc:
            logger.warning("Ошибка остановки Playwright: %s", exc)
        finally:
            self._playwright = None

    async def start(self) -> Path:
        """
        Запускает полный цикл записи встречи.

        Returns:
            Абсолютный путь к сохранённому аудиофайлу.
        """
        self._config.output_dir.mkdir(parents=True, exist_ok=True)

        if self._config.debug:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._debug_dir = self._config.output_dir / f"debug_{ts}"
            self._debug_dir.mkdir(parents=True, exist_ok=True)

        self._temp_dir = Path(tempfile.mkdtemp(prefix="telemost_audio_"))
        self._webrtc_webm_path = self._temp_dir / "webrtc_audio.webm"

        try:
            self._playwright = await async_playwright().start()
            headed = _use_headed_browser(self._config.debug)
            if self._config.debug and not headed:
                logger.info(
                    "DISPLAY не задан — debug работает в headless со скриншотами и логом DOM"
                )
                print(
                    "🔧 Debug: headless + скриншоты (на сервере без X11 headed недоступен)",
                    flush=True,
                )

            chromium_args = [
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-software-rasterizer",
                # Авто-разрешение getUserMedia без UI-диалога в headless
                "--use-fake-ui-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
            ]
            # headless=new — новый режим Chromium без GUI, стабильнее для Docker
            if not headed:
                chromium_args.append("--headless=new")

            self._browser = await self._playwright.chromium.launch(
                headless=not headed,
                args=chromium_args,
            )

            self._context = await self._browser.new_context(
                permissions=["microphone", "camera"],
            )
            await install_audio_capture(self._context)

            self._page = await self._context.new_page()
            self._recording_started = True

            def on_page_close() -> None:
                self._page_closed_event.set()

            self._page.on("close", on_page_close)

            self._status("🔗 Открытие встречи...")
            await self._page.goto(
                self._config.meeting_url,
                timeout=30_000,
                wait_until="domcontentloaded",
            )
            await self._debug_screenshot("after_navigation")

            await assert_meeting_not_ended(self._page, phase="navigation")
            frames = await ensure_audio_capture(self._page)
            logger.debug("Аудиозахват внедрён в %d фрейм(ов)", frames)

            # Даём странице время отрендерить предкомнату
            await asyncio.sleep(2)
            await self._debug_screenshot("prejoin_room")

            if await self._detect_auth_required():
                await self._save_auth_artifacts()
                print("❌ Встреча требует авторизации. Анонимный вход недоступен.")
                await self._close_browser()
                await self._cancel_chunk_flush()
                self._cleanup_temp_dir()
                sys.exit(2)

            self._status("👤 Ввод имени...")
            await fill_name_and_join(
                self._page,
                self._config.bot_name,
                debug=self._config.debug,
                debug_dir=self._debug_dir or self._config.output_dir,
            )
            await self._debug_screenshot("after_join")

            frames = await ensure_audio_capture(self._page)
            logger.info("Аудиозахват после входа: %d фрейм(ов)", frames)

            self._status("✅ Подключение к встрече...")
            await self._debug_screenshot("meeting_active")

            if self._config.debug:
                status = await get_capture_status(self._page)
                installed = status.get("installed", "?")
                print(
                    f"🎙 Аудиозахват: tracks={status.get('trackCount', 0)}, "
                    f"recorder={status.get('recorderState', 'none')}, "
                    f"hooks={installed}",
                    flush=True,
                )

            self._status("⏺ Запись начата")
            self._chunk_flush_task = asyncio.create_task(self._periodic_chunk_flush())

            reason = await self._wait_for_meeting_end()
            self._stop_reason = reason

            if reason == "signal":
                self._status("⏹ Запись остановлена по сигналу")
            elif reason == "max_duration":
                self._status("⏹ Достигнут лимит длительности записи")
            elif reason == "page_closed":
                self._status("⏹ Страница встречи закрыта")
            elif reason == "meeting_ended":
                self._status("⏹ Встреча завершена")
            else:
                self._status("⏹ Встреча завершена")

            audio_path = await self._finalize_recording(reason)
            if audio_path is None:
                raise RuntimeError("Не удалось сохранить запись встречи")

            return audio_path

        except MeetingEndedError as exc:
            await self._debug_screenshot("meeting_ended")
            self._status("⏹ Встреча завершена")
            print(f"⏹ {exc}", flush=True)
            audio_path = await self._finalize_recording("meeting_ended")
            if audio_path:
                print(f"💾 Сохранено: {audio_path}", flush=True)
                return audio_path
            await self._close_browser()
            await self._cancel_chunk_flush()
            self._cleanup_temp_dir()
            raise
        except DomScannerError:
            await self._debug_screenshot("dom_error")
            await self._close_browser()
            await self._cancel_chunk_flush()
            self._cleanup_temp_dir()
            raise
        except Exception:
            if self._recording_started and not self._finalize_done:
                try:
                    await self._finalize_recording("error")
                except Exception as finalize_exc:
                    logger.warning("Не удалось сохранить аудиозапись: %s", finalize_exc)
            elif not self._finalize_done:
                await self._close_browser()
                self._cleanup_temp_dir()
            raise

    async def stop(self, reason: str = "signal") -> Path | None:
        """
        Принудительно останавливает запись (graceful shutdown).

        Args:
            reason: Причина остановки.

        Returns:
            Путь к аудиофайлу или None.
        """
        self._stop_reason = reason
        self._shutdown_event.set()

        if not self._recording_started:
            await self._close_browser()
            return None

        return await self._finalize_recording(reason)
