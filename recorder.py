"""Запись встречи Яндекс.Телемост через Playwright с CDP-видеозаписью."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from audio_extractor import AudioExtractionError, extract_audio
from dom_scanner import DomScannerError, fill_name_and_join

logger = logging.getLogger(__name__)

MEETING_END_PATTERN = re.compile(r"встреча завершена|meeting ended", re.IGNORECASE)
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
    video_resolution: tuple[int, int]
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
        self._temp_video_dir: Path | None = None
        self._debug_dir: Path | None = None
        self._step_counter = 0
        self._recording_started = False
        self._stop_reason = "normal"
        self._page_closed_event = asyncio.Event()

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
                                return "normal"
                except Exception:
                    pass
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
                        return "normal"

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

    async def _get_video_path(self) -> Path | None:
        """Получает путь к видеофайлу до закрытия контекста."""
        if not self._page or not self._page.video:
            return None
        try:
            raw_path = await self._page.video.path()
            return Path(raw_path) if raw_path else None
        except Exception as exc:
            logger.warning("Не удалось получить путь к видео: %s", exc)
            return None

    async def _finalize_recording(self, reason: str) -> Path | None:
        """Закрывает браузер и извлекает аудио из записи."""
        video_path: Path | None = None

        try:
            video_path = await self._get_video_path()
        except Exception as exc:
            logger.warning("Ошибка при получении видео: %s", exc)

        await self._close_browser()

        if not video_path or not video_path.exists():
            # Поиск webm во временной директории как fallback
            if self._temp_video_dir and self._temp_video_dir.exists():
                webm_files = list(self._temp_video_dir.glob("*.webm"))
                if webm_files:
                    video_path = webm_files[0]

        if not video_path or not video_path.exists():
            logger.error("Видеозапись не найдена (причина: %s)", reason)
            return None

        output_path = self._config.output_dir / self._config.output_filename
        self._status("🎵 Извлечение аудио...")

        try:
            audio_path = await extract_audio(
                video_path,
                output_path,
                self._config.audio_format,  # type: ignore[arg-type]
            )
            return audio_path
        except AudioExtractionError as exc:
            logger.error("Ошибка извлечения аудио: %s", exc)
            # Сохраняем видео как частичную запись при ошибке FFmpeg
            partial_path = self._config.output_dir / f"partial_{video_path.name}"
            if not partial_path.exists():
                try:
                    video_path.rename(partial_path)
                    logger.info("Частичная видеозапись сохранена: %s", partial_path)
                except OSError:
                    pass
            raise

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

        self._temp_video_dir = Path(tempfile.mkdtemp(prefix="telemost_video_"))
        width, height = self._config.video_resolution

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
                record_video_dir=str(self._temp_video_dir),
                record_video_size={"width": width, "height": height},
                # Разрешаем воспроизведение медиа для захвата звука встречи
                permissions=["microphone", "camera"],
            )

            self._page = await self._context.new_page()

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

            # Даём странице время отрендерить предкомнату
            await asyncio.sleep(2)
            await self._debug_screenshot("prejoin_room")

            if await self._detect_auth_required():
                await self._save_auth_artifacts()
                print("❌ Встреча требует авторизации. Анонимный вход недоступен.")
                await self._close_browser()
                sys.exit(2)

            self._status("👤 Ввод имени...")
            await fill_name_and_join(
                self._page,
                self._config.bot_name,
                debug=self._config.debug,
                debug_dir=self._debug_dir or self._config.output_dir,
            )
            await self._debug_screenshot("after_join")

            self._status("✅ Подключение к встрече...")
            await asyncio.sleep(3)
            await self._debug_screenshot("meeting_active")

            self._recording_started = True
            self._status("⏺ Запись начата")

            reason = await self._wait_for_meeting_end()
            self._stop_reason = reason

            if reason == "signal":
                self._status("⏹ Запись остановлена по сигналу")
            elif reason == "max_duration":
                self._status("⏹ Достигнут лимит длительности записи")
            elif reason == "page_closed":
                self._status("⏹ Страница встречи закрыта")
            else:
                self._status("⏹ Встреча завершена")

            audio_path = await self._finalize_recording(reason)
            if audio_path is None:
                raise RuntimeError("Не удалось сохранить запись встречи")

            return audio_path

        except DomScannerError:
            await self._debug_screenshot("dom_error")
            await self._close_browser()
            raise
        except Exception:
            # При любой ошибке пытаемся сохранить частичную запись
            if self._recording_started:
                try:
                    await self._finalize_recording("error")
                except Exception as partial_exc:
                    logger.warning("Не удалось сохранить частичную запись: %s", partial_exc)
            else:
                await self._close_browser()
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
