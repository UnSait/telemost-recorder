"""Извлечение аудиодорожки из видеозаписи Playwright через FFmpeg."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

AudioFormat = Literal["opus", "mp3"]


class AudioExtractionError(Exception):
    """Ошибка при извлечении аудио через FFmpeg."""


async def extract_audio(
    video_path: Path,
    output_path: Path,
    fmt: AudioFormat,
) -> Path:
    """
    Извлекает аудиодорожку из WebM-видео и удаляет исходный файл.

    Args:
        video_path: Путь к временному .webm от Playwright.
        output_path: Путь для сохранения аудиофайла.
        fmt: Формат выходного файла — opus (zero-copy) или mp3.

    Returns:
        Абсолютный путь к сохранённому аудиофайлу.

    Raises:
        AudioExtractionError: Если файл отсутствует, пуст или FFmpeg завершился с ошибкой.
    """
    if not video_path.exists():
        raise AudioExtractionError(
            f"Видеофайл не найден: {video_path}. Запись могла не начаться."
        )

    if video_path.stat().st_size == 0:
        raise AudioExtractionError(
            f"Видеофайл пуст (0 байт): {video_path}. Запись не содержит данных."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Opus: zero-copy — без перекодирования, быстрее и без потери качества
    if fmt == "opus":
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-acodec",
            "copy",
            str(output_path),
        ]
    else:
        # MP3: перекодирование через libmp3lame для совместимости
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-acodec",
            "libmp3lame",
            "-b:a",
            "128k",
            str(output_path),
        ]

    logger.info("Запуск FFmpeg: %s", " ".join(cmd))

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr_bytes = await process.communicate()
    stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

    if process.returncode != 0:
        if "does not contain any stream" in stderr_text or "no audio streams" in stderr_text.lower():
            raise AudioExtractionError(
                "В записи отсутствует аудиодорожка. "
                "WebRTC-захват не получил звук встречи (tracks=0). "
                f"Детали FFmpeg: {stderr_text[-500:]}"
            )
        raise AudioExtractionError(
            f"FFmpeg завершился с кодом {process.returncode}. "
            f"Детали: {stderr_text[-500:]}"
        )

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise AudioExtractionError(
            f"Аудиофайл не создан или пуст: {output_path}"
        )

    # Удаляем временный WebM после успешного извлечения
    try:
        video_path.unlink()
        logger.debug("Временный видеофайл удалён: %s", video_path)
    except OSError as exc:
        logger.warning("Не удалось удалить временный файл %s: %s", video_path, exc)

    resolved = output_path.resolve()
    logger.info("Аудио сохранено: %s (%d байт)", resolved, resolved.stat().st_size)
    return resolved
