"""Снимки потребления RAM/CPU процессом бота и дочерним Chromium (режим --debug)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]


@dataclass(frozen=True)
class ResourceSnapshot:
    """Потребление ресурсов деревом процессов от Python до Chromium."""

    python_ram_mb: float
    chromium_ram_mb: float
    total_ram_mb: float
    cpu_percent: float
    child_count: int
    disk_written_mb: float


def take_snapshot(root_pid: int | None = None) -> ResourceSnapshot | None:
    """
    Снимок RAM/CPU для текущего процесса и всех дочерних (Chromium).

    Returns:
        None, если psutil недоступен или процесс завершился.
    """
    if psutil is None:
        return None

    pid = root_pid or os.getpid()
    try:
        proc = psutil.Process(pid)
        children = proc.children(recursive=True)

        python_ram = proc.memory_info().rss
        chromium_ram = sum(c.memory_info().rss for c in children)
        io_counters = proc.io_counters()
        disk_written = io_counters.write_bytes
        for child in children:
            try:
                disk_written += child.io_counters().write_bytes
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        cpu = proc.cpu_percent(interval=0.0)
        for child in children:
            try:
                cpu += child.cpu_percent(interval=0.0)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        mb = 1024 * 1024
        return ResourceSnapshot(
            python_ram_mb=python_ram / mb,
            chromium_ram_mb=chromium_ram / mb,
            total_ram_mb=(python_ram + chromium_ram) / mb,
            cpu_percent=cpu,
            child_count=len(children),
            disk_written_mb=disk_written / mb,
        )
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        logger.debug("Не удалось снять snapshot ресурсов: %s", exc)
        return None


def format_snapshot(snapshot: ResourceSnapshot) -> str:
    """Форматирует снимок для stdout в --debug."""
    return (
        f"📊 RAM: Python {snapshot.python_ram_mb:.0f} MB + "
        f"Chromium {snapshot.chromium_ram_mb:.0f} MB = "
        f"{snapshot.total_ram_mb:.0f} MB | "
        f"CPU ~{snapshot.cpu_percent:.0f}% | "
        f"процессов {snapshot.child_count + 1} | "
        f"запись на диск ~{snapshot.disk_written_mb:.0f} MB"
    )
