"""Семантический поиск элементов предкомнаты Яндекс.Телемост без CSS-селекторов."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from playwright.async_api import Locator, Page

logger = logging.getLogger(__name__)

# Паттерны для семантического поиска — централизованы для простой адаптации при смене UI
NAME_PATTERN = re.compile(r"имя|name|представьтесь|ваше имя", re.IGNORECASE)
# «Подключиться» в Телемосте; намеренно без голого «войти» — это кнопка авторизации в сайдбаре
JOIN_PATTERN = re.compile(r"подключиться|присоединиться|join|enter", re.IGNORECASE)
LOGIN_BUTTON_PATTERN = re.compile(r"^(войти|sign in|log in|авториз)", re.IGNORECASE)

# Признаки активной встречи (НЕ предкомнаты: там «Включить камеру», а не «Выключить»)
IN_MEETING_CONTROLS_PATTERN = re.compile(
    r"выключить микрофон|отключить микрофон|"
    r"выключить камеру|отключить камеру|"
    r"завершить встречу|покинуть встречу|"
    r"leave meeting|end meeting",
    re.IGNORECASE,
)
PREJOIN_JOIN_VISIBLE_PATTERN = re.compile(r"подключиться|присоединиться", re.IGNORECASE)
WAITING_ROOM_PATTERN = re.compile(
    r"ожидайте|организатор впустит|зал ожидания|waiting room|wait for the host",
    re.IGNORECASE,
)


class DomScannerError(Exception):
    """Элементы предкомнаты не найдены за отведённое время."""

    def __init__(self, message: str, candidates: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.candidates = candidates or []


@dataclass
class ElementCandidate:
    """Описание найденного DOM-элемента для диагностики."""

    tag: str
    text: str
    aria_label: str
    placeholder: str
    bbox: dict[str, float] | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def _get_element_info(handle: Any) -> ElementCandidate:
    """Собирает метаданные элемента для отладки и сообщений об ошибках."""
    tag = await handle.evaluate("el => el.tagName.toLowerCase()")
    text = (await handle.inner_text() if tag != "input" else "") or ""
    text = text.strip()[:120]

    attrs = await handle.evaluate(
        """el => ({
            ariaLabel: el.getAttribute('aria-label') || '',
            placeholder: el.getAttribute('placeholder') || '',
        })"""
    )
    bbox = await handle.bounding_box()

    return ElementCandidate(
        tag=tag,
        text=text,
        aria_label=attrs.get("ariaLabel", ""),
        placeholder=attrs.get("placeholder", ""),
        bbox=bbox,
    )


async def collect_input_candidates(page: Page) -> list[ElementCandidate]:
    """Собирает всех кандидатов на поле ввода имени."""
    candidates: list[ElementCandidate] = []

    textboxes = page.get_by_role("textbox")
    count = await textboxes.count()
    for i in range(count):
        try:
            handle = textboxes.nth(i)
            if await handle.is_visible():
                candidates.append(await _get_element_info(handle))
        except Exception:
            continue

    # Fallback: input[type="text"] — единственный допустимый type-селектор по ТЗ
    inputs = page.locator('input[type="text"]')
    input_count = await inputs.count()
    for i in range(input_count):
        try:
            handle = inputs.nth(i)
            if await handle.is_visible():
                info = await _get_element_info(handle)
                if info not in candidates:
                    candidates.append(info)
        except Exception:
            continue

    return candidates


async def collect_button_candidates(page: Page) -> list[ElementCandidate]:
    """Собирает всех кандидатов на кнопку подключения."""
    candidates: list[ElementCandidate] = []

    buttons = page.locator("button")
    count = await buttons.count()
    for i in range(count):
        try:
            handle = buttons.nth(i)
            if await handle.is_visible():
                candidates.append(await _get_element_info(handle))
        except Exception:
            continue

    return candidates


def _matches_name_pattern(candidate: ElementCandidate) -> bool:
    """Проверяет, подходит ли элемент под паттерн поля имени."""
    searchable = " ".join(
        filter(None, [candidate.text, candidate.aria_label, candidate.placeholder])
    )
    return bool(NAME_PATTERN.search(searchable))


def _matches_join_pattern(candidate: ElementCandidate) -> bool:
    """Проверяет, подходит ли элемент под паттерн кнопки входа в встречу."""
    searchable = " ".join(filter(None, [candidate.text, candidate.aria_label]))
    if LOGIN_BUTTON_PATTERN.search(candidate.aria_label.strip()):
        return False
    if LOGIN_BUTTON_PATTERN.search(candidate.text.strip()):
        return False
    return bool(JOIN_PATTERN.search(searchable))


def _join_button_score(candidate: ElementCandidate) -> int:
    """Чем выше счёт, тем вероятнее это основная кнопка «Подключиться»."""
    score = 0
    text = candidate.text.strip()
    aria = candidate.aria_label.strip()

    if PREJOIN_JOIN_VISIBLE_PATTERN.search(text):
        score += 100
    if PREJOIN_JOIN_VISIBLE_PATTERN.search(aria):
        score += 80
    if JOIN_PATTERN.search(text) or JOIN_PATTERN.search(aria):
        score += 40

    width = (candidate.bbox or {}).get("width", 0)
    if width >= 200:
        score += 30
    elif width >= 120:
        score += 15

    return score


async def find_name_input(page: Page) -> Locator | None:
    """
    Ищет поле ввода имени в предкомнате семантически.

    Приоритет: role=textbox с текстом/label → input[type=text] с placeholder.
    """
    # Основной путь: textbox с семантическим фильтром
    textboxes = page.get_by_role("textbox")
    count = await textboxes.count()
    for i in range(count):
        locator = textboxes.nth(i)
        try:
            if not await locator.is_visible():
                continue
            info = await _get_element_info(locator)
            if _matches_name_pattern(info):
                return locator
        except Exception:
            continue

    # Fallback: перебор input[type="text"] по placeholder
    inputs = page.locator('input[type="text"]')
    input_count = await inputs.count()
    for i in range(input_count):
        locator = inputs.nth(i)
        try:
            if not await locator.is_visible():
                continue
            info = await _get_element_info(locator)
            if _matches_name_pattern(info):
                return locator
        except Exception:
            continue

    # Последний fallback: первый видимый textbox (если на странице только одно поле)
    for i in range(count):
        locator = textboxes.nth(i)
        try:
            if await locator.is_visible():
                return locator
        except Exception:
            continue

    return None


async def find_join_button(page: Page) -> Locator | None:
    """
    Ищет кнопку подключения к встрече семантически.

    Приоритет: «Подключиться»/«Присоединиться» с широкой кнопкой;
    исключает сайдбарную «Войти» (авторизация).
    """
    best_locator: Locator | None = None
    best_score = -1

    all_buttons = page.locator("button")
    btn_count = await all_buttons.count()
    for i in range(btn_count):
        locator = all_buttons.nth(i)
        try:
            if not await locator.is_visible():
                continue
            info = await _get_element_info(locator)
            if not _matches_join_pattern(info):
                continue
            score = _join_button_score(info)
            if score > best_score:
                best_score = score
                best_locator = locator
        except Exception:
            continue

    return best_locator


async def _is_prejoin_screen_visible(page: Page) -> bool:
    """Проверяет, видна ли ещё кнопка «Подключиться» (значит, встреча не начата)."""
    buttons = page.locator("button")
    count = await buttons.count()
    for i in range(count):
        try:
            button = buttons.nth(i)
            if not await button.is_visible():
                continue
            info = await _get_element_info(button)
            if PREJOIN_JOIN_VISIBLE_PATTERN.search(info.text):
                return True
        except Exception:
            continue
    return False


async def _is_waiting_room_visible(page: Page) -> bool:
    """Проверяет экран зала ожидания (организатор должен впустить)."""
    waiting = page.get_by_text(WAITING_ROOM_PATTERN)
    if await waiting.count() == 0:
        return False
    for i in range(await waiting.count()):
        if await waiting.nth(i).is_visible():
            return True
    return False


def _format_candidates(candidates: list[ElementCandidate]) -> str:
    """Форматирует список кандидатов для вывода в ошибке."""
    if not candidates:
        return "  (кандидаты не найдены)"
    lines = []
    for idx, c in enumerate(candidates, 1):
        lines.append(
            f"  {idx}. <{c.tag}> text={c.text!r} aria-label={c.aria_label!r} "
            f"placeholder={c.placeholder!r} bbox={c.bbox}"
        )
    return "\n".join(lines)


async def _save_debug_artifacts(page: Page, output_dir: Path, prefix: str) -> tuple[Path, Path]:
    """Сохраняет скриншот и HTML при ошибке поиска элементов."""
    output_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = output_dir / f"{prefix}.png"
    html_path = output_dir / f"{prefix}.html"

    await page.screenshot(path=str(screenshot_path), full_page=True)
    html_content = await page.content()
    html_path.write_text(html_content, encoding="utf-8")

    return screenshot_path, html_path


async def _wait_for_active_meeting(page: Page, timeout_ms: int = 30_000) -> None:
    """Ждёт перехода из предкомнаты в активную встречу."""
    deadline = time.monotonic() + timeout_ms / 1000
    saw_waiting_room = False

    while time.monotonic() < deadline:
        # Всё ещё предкомната — кнопка «Подключиться» на месте
        if await _is_prejoin_screen_visible(page):
            await asyncio.sleep(0.5)
            continue

        if await _is_waiting_room_visible(page):
            if not saw_waiting_room:
                print("⏳ Зал ожидания — ждём, пока организатор впустит...", flush=True)
            saw_waiting_room = True
            logger.info("Обнаружен зал ожидания — ждём, пока организатор впустит...")
            await asyncio.sleep(1.0)
            continue

        # Признак 1: кнопки управления внутри встречи (выключить микрофон/камеру)
        controls = page.get_by_text(IN_MEETING_CONTROLS_PATTERN)
        if await controls.count() > 0:
            for i in range(await controls.count()):
                if await controls.nth(i).is_visible():
                    return

        # Признак 2: таймер встречи (MM:SS), только если предкомната уже ушла
        timer = page.get_by_text(re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$"))
        if await timer.count() > 0:
            for i in range(await timer.count()):
                if await timer.nth(i).is_visible():
                    return

        if saw_waiting_room:
            # Были в зале ожидания, кнопка подключиться исчезла — вероятно впустили
            return

        await asyncio.sleep(0.5)

    raise DomScannerError(
        "Не удалось подтвердить вход в активную встречу за "
        f"{timeout_ms // 1000} секунд. "
        "Возможно, организатор не впустил из зала ожидания."
    )


async def fill_name_and_join(
    page: Page,
    bot_name: str,
    debug: bool = False,
    debug_dir: Path | None = None,
) -> None:
    """
    Заполняет имя в предкомнате и нажимает кнопку подключения.

    Args:
        page: Страница Playwright.
        bot_name: Имя для отображения в списке участников.
        debug: Логировать всех кандидатов перед действиями.
        debug_dir: Директория для сохранения артефактов при ошибке.

    Raises:
        DomScannerError: Если элементы не найдены или вход не подтверждён.
    """
    deadline = time.monotonic() + 20.0
    name_input: Locator | None = None

    while time.monotonic() < deadline:
        input_candidates = await collect_input_candidates(page)
        if debug:
            print("🔍 Кандидаты на поле имени:")
            print(_format_candidates(input_candidates))

        name_input = await find_name_input(page)
        if name_input is not None:
            break
        await asyncio.sleep(0.5)

    if name_input is None:
        input_candidates = await collect_input_candidates(page)
        button_candidates = await collect_button_candidates(page)
        all_candidates = [c.to_dict() for c in input_candidates + button_candidates]

        if debug_dir:
            await _save_debug_artifacts(page, debug_dir, "dom_scanner_failed")

        raise DomScannerError(
            "Поле ввода имени не найдено за 20 секунд.\n"
            f"Кандидаты input:\n{_format_candidates(input_candidates)}\n"
            f"Кандидаты button:\n{_format_candidates(button_candidates)}",
            candidates=all_candidates,
        )

    await name_input.fill(bot_name)
    logger.info("Имя заполнено: %s", bot_name)

    join_button: Locator | None = None
    while time.monotonic() < deadline:
        button_candidates = await collect_button_candidates(page)
        if debug:
            print("🔍 Кандидаты на кнопку подключения:")
            print(_format_candidates(button_candidates))

        join_button = await find_join_button(page)
        if join_button is not None:
            break
        await asyncio.sleep(0.5)

    if join_button is None:
        input_candidates = await collect_input_candidates(page)
        button_candidates = await collect_button_candidates(page)
        all_candidates = [c.to_dict() for c in input_candidates + button_candidates]

        if debug_dir:
            await _save_debug_artifacts(page, debug_dir, "join_button_failed")

        raise DomScannerError(
            "Кнопка подключения не найдена за 20 секунд.\n"
            f"Кандидаты input:\n{_format_candidates(input_candidates)}\n"
            f"Кандидаты button:\n{_format_candidates(button_candidates)}",
            candidates=all_candidates,
        )

    await join_button.click()
    logger.info("Нажата кнопка подключения")
    # Даём странице обработать клик и возможный переход в зал ожидания
    await asyncio.sleep(1.0)

    try:
        await _wait_for_active_meeting(page, timeout_ms=30_000)
    except DomScannerError:
        input_candidates = await collect_input_candidates(page)
        button_candidates = await collect_button_candidates(page)
        all_candidates = [c.to_dict() for c in input_candidates + button_candidates]

        if debug_dir:
            await _save_debug_artifacts(page, debug_dir, "meeting_join_failed")

        raise DomScannerError(
            "Не удалось подтвердить вход в встречу после нажатия кнопки.\n"
            f"Кандидаты input:\n{_format_candidates(input_candidates)}\n"
            f"Кандидаты button:\n{_format_candidates(button_candidates)}",
            candidates=all_candidates,
        ) from None
