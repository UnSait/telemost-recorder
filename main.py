#!/usr/bin/env python3
"""CLI-инструмент для анонимной записи встреч Яндекс.Телемост."""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import signal
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from audio_extractor import AudioExtractionError
from dom_scanner import DomScannerError, MeetingEndedError
from recorder import RecorderConfig, TelemostRecorder

logger = logging.getLogger(__name__)

# Меняйте при каждом релизе — по этой строке видно, что образ пересобран
BUILD_VERSION = "2026-07-02-csat-v4"

TELEMOST_URL_PATTERN = re.compile(r"telemost\.yandex", re.IGNORECASE)


def parse_video_resolution(value: str) -> tuple[int, int]:
    """Парсит разрешение видео из формата WIDTHxHEIGHT."""
    match = re.match(r"^(\d+)x(\d+)$", value.strip())
    if not match:
        raise argparse.ArgumentTypeError(
            f"Некорректное разрешение '{value}'. Ожидается формат: 640x360"
        )
    width, height = int(match.group(1)), int(match.group(2))
    if width < 320 or height < 240 or width > 1920 or height > 1080:
        raise argparse.ArgumentTypeError(
            f"Разрешение {width}x{height} вне допустимого диапазона (320x240 — 1920x1080)"
        )
    return width, height


def validate_meeting_url(url: str) -> str:
    """
    Валидирует URL встречи Яндекс.Телемост.

    Raises:
        SystemExit: При невалидном URL (код 1).
    """
    parsed = urlparse(url)

    if parsed.scheme != "https":
        print(f"❌ Ошибка: URL должен использовать HTTPS. Получено: {parsed.scheme or '(нет)'}")
        sys.exit(1)

    if not parsed.netloc or not TELEMOST_URL_PATTERN.search(parsed.netloc):
        print(
            f"❌ Ошибка: URL должен указывать на telemost.yandex.ru. "
            f"Получено: {parsed.netloc or url}"
        )
        sys.exit(1)

    if not parsed.path or parsed.path == "/":
        print("❌ Ошибка: URL не содержит идентификатор встречи.")
        sys.exit(1)

    return url


def extract_meeting_id(url: str) -> str:
    """Извлекает идентификатор встречи из URL."""
    path = urlparse(url).path.rstrip("/")
    meeting_id = path.split("/")[-1]
    # Очистка от небезопасных символов для имени файла
    return re.sub(r"[^\w\-]", "_", meeting_id)[:64] or "meeting"


def build_output_filename(meeting_url: str, audio_format: str) -> str:
    """Формирует имя выходного аудиофайла."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    meeting_id = extract_meeting_id(meeting_url)
    return f"{ts}_{meeting_id}.{audio_format}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Парсит аргументы командной строки."""
    parser = argparse.ArgumentParser(
        description="Анонимная запись встречи Яндекс.Телемост в Docker-контейнере",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Пример:\n"
            '  python main.py "https://telemost.yandex.ru/j/1234567890"\n'
            '  python main.py "https://telemost.yandex.ru/j/123" --format mp3 --debug'
        ),
    )

    parser.add_argument(
        "meeting_url",
        help="Ссылка на встречу Яндекс.Телемост",
    )
    parser.add_argument(
        "--output-dir",
        default="/app/recordings",
        help="Директория для сохранения записей (по умолчанию: /app/recordings)",
    )
    parser.add_argument(
        "--bot-name",
        default="🤖 AI Ассистент",
        help='Имя в предкомнате (по умолчанию: "🤖 AI Ассистент")',
    )
    parser.add_argument(
        "--max-duration",
        type=int,
        default=14400,
        help="Максимальная длительность записи в секундах (по умолчанию: 14400 = 4 часа)",
    )
    parser.add_argument(
        "--format",
        choices=["opus", "mp3"],
        default="opus",
        dest="audio_format",
        help="Формат выходного аудио (по умолчанию: opus)",
    )
    parser.add_argument(
        "--video-resolution",
        type=parse_video_resolution,
        default=(640, 360),
        help="Разрешение видеозаписи WIDTHxHEIGHT (по умолчанию: 640x360)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Отладка: подробные логи, скриншоты, лог DOM-кандидатов (headed только при $DISPLAY)",
    )

    return parser.parse_args(argv)


async def run_recorder(args: argparse.Namespace) -> int:
    """Основной асинхронный цикл записи."""
    meeting_url = validate_meeting_url(args.meeting_url)
    output_dir = Path(args.output_dir)
    output_filename = build_output_filename(meeting_url, args.audio_format)

    shutdown_event = asyncio.Event()
    received_signal: list[int] = []

    recorder: TelemostRecorder | None = None

    def handle_signal(signum: int) -> None:
        sig_name = signal.Signals(signum).name
        logger.warning("Получен сигнал %s, инициируем graceful shutdown...", sig_name)
        received_signal.append(signum)
        shutdown_event.set()

    loop = asyncio.get_running_loop()

    # Регистрация обработчиков сигналов (Linux/Docker)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_signal, sig)
        except (NotImplementedError, RuntimeError):
            # Fallback для Windows при локальной разработке
            signal.signal(sig, lambda s, _f, sn=sig: handle_signal(sn))

    def on_status(message: str) -> None:
        print(message, flush=True)

    config = RecorderConfig(
        meeting_url=meeting_url,
        bot_name=args.bot_name,
        output_dir=output_dir,
        max_duration=args.max_duration,
        audio_format=args.audio_format,
        video_resolution=args.video_resolution,
        debug=args.debug,
        output_filename=output_filename,
        on_status=on_status,
    )

    recorder = TelemostRecorder(config, shutdown_event=shutdown_event)

    record_task = asyncio.create_task(recorder.start())

    # Ожидаем завершения записи или сигнала
    shutdown_waiter = asyncio.create_task(shutdown_event.wait())

    done, pending = await asyncio.wait(
        {record_task, shutdown_waiter},
        return_when=asyncio.FIRST_COMPLETED,
    )

    exit_code = 0

    if shutdown_waiter in done and not record_task.done():
        # Graceful shutdown по сигналу
        print("⚠️ Получен сигнал остановки, сохраняем частичную запись...", flush=True)
        audio_path = await recorder.stop(reason="signal")
        record_task.cancel()
        try:
            await record_task
        except (asyncio.CancelledError, Exception):
            pass

        if audio_path:
            print(f"💾 Частичная запись сохранена: {audio_path}", flush=True)
        else:
            print("⚠️ Частичная запись не удалась — видеофайл отсутствует", flush=True)

        sig = received_signal[0] if received_signal else signal.SIGINT
        exit_code = 130 if sig == signal.SIGINT else 143
    else:
        shutdown_waiter.cancel()
        try:
            audio_path = await record_task
            print(f"💾 Сохранено: {audio_path}", flush=True)
        except DomScannerError as exc:
            print(f"❌ Ошибка поиска элементов предкомнаты:\n{exc}", flush=True)
            exit_code = 1
        except MeetingEndedError as exc:
            print(f"⏹ {exc}", flush=True)
            if exc.phase == "join":
                print(
                    "ℹ️ Встреча уже завершена или ссылка недействительна. "
                    "Запустите бота до окончания встречи.",
                    flush=True,
                )
            exit_code = 3
        except AudioExtractionError as exc:
            print(f"❌ Ошибка извлечения аудио: {exc}", flush=True)
            exit_code = 1
        except SystemExit as exc:
            raise
        except Exception as exc:
            logger.exception("Непредвиденная ошибка")
            print(f"❌ Ошибка записи: {exc}", flush=True)
            exit_code = 1

    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    return exit_code


def main(argv: list[str] | None = None) -> None:
    """Точка входа CLI."""
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if args.max_duration < 60:
        print("❌ Ошибка: --max-duration должен быть не менее 60 секунд.")
        sys.exit(1)

    print(f"📦 telemost-recorder {BUILD_VERSION}", flush=True)

    try:
        exit_code = asyncio.run(run_recorder(args))
    except KeyboardInterrupt:
        print("\n⚠️ Прервано пользователем (Ctrl+C)", flush=True)
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as exc:
        logger.exception("Критическая ошибка")
        print(f"❌ Критическая ошибка: {exc}", flush=True)
        sys.exit(1)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
