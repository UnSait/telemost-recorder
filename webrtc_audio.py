"""Захват звука встречи через WebRTC/MediaRecorder внутри страницы."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page

logger = logging.getLogger(__name__)

# Перехват RTCPeerConnection и <video>/<audio> — Playwright video не пишет звук вкладки
AUDIO_CAPTURE_INIT_SCRIPT = """
(() => {
  if (window.__telemostAudioCapture) return;
  window.__telemostAudioCapture = {
    chunks: [],
    tracks: new Map(),
    recorder: null,
  };

  const addTrack = (track) => {
    if (!track || track.kind !== 'audio') return;
    const cap = window.__telemostAudioCapture;
    cap.tracks.set(track.id, track);
    track.addEventListener('ended', () => cap.tracks.delete(track.id));
    maybeStartRecorder();
  };

  const maybeStartRecorder = () => {
    const cap = window.__telemostAudioCapture;
    if (cap.recorder && cap.recorder.state === 'recording') return;

    const tracks = [...cap.tracks.values()].filter((t) => t.readyState === 'live');
    if (!tracks.length) return;

    const stream = new MediaStream(tracks);
    const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
      ? 'audio/webm;codecs=opus'
      : 'audio/webm';

    const recorder = new MediaRecorder(stream, {
      mimeType: mime,
      audioBitsPerSecond: 128000,
    });

    cap.chunks = [];
    recorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) cap.chunks.push(event.data);
    };
    recorder.start(1000);
    cap.recorder = recorder;
  };

  const hookPeerConnection = (OrigPC) => {
    if (!OrigPC) return;
    const Wrapped = function (...args) {
      const pc = new OrigPC(...args);
      pc.addEventListener('track', (event) => addTrack(event.track));
      return pc;
    };
    Wrapped.prototype = OrigPC.prototype;
    return Wrapped;
  };

  if (window.RTCPeerConnection) {
    window.RTCPeerConnection = hookPeerConnection(window.RTCPeerConnection);
  }
  if (window.webkitRTCPeerConnection) {
    window.webkitRTCPeerConnection = hookPeerConnection(window.webkitRTCPeerConnection);
  }

  const hookMediaElement = (element) => {
    if (!element || element.__telemostHooked) return;
    element.__telemostHooked = true;

    const captureFromElement = () => {
      const stream = element.srcObject;
      if (stream && typeof stream.getAudioTracks === 'function') {
        stream.getAudioTracks().forEach(addTrack);
      }
      maybeStartRecorder();
    };

    element.addEventListener('loadedmetadata', captureFromElement);
    element.addEventListener('play', captureFromElement);
    captureFromElement();
  };

  const scanMediaElements = () => {
    document.querySelectorAll('video,audio').forEach(hookMediaElement);
  };

  const observer = new MutationObserver(scanMediaElements);
  observer.observe(document.documentElement, { childList: true, subtree: true });
  scanMediaElements();

  window.__telemostAudioCaptureStatus = () => {
    const cap = window.__telemostAudioCapture;
    return {
      trackCount: cap.tracks.size,
      recorderState: cap.recorder ? cap.recorder.state : 'none',
      chunkCount: cap.chunks.length,
    };
  };

  window.__telemostStopAudioCapture = () => new Promise((resolve) => {
    const cap = window.__telemostAudioCapture;
    const recorder = cap.recorder;

    if (!recorder || recorder.state === 'inactive') {
      resolve({
        bytes: [],
        byteLength: 0,
        trackCount: cap.tracks.size,
        hadRecorder: false,
      });
      return;
    }

    recorder.onstop = async () => {
      const blob = new Blob(cap.chunks, {
        type: recorder.mimeType || 'audio/webm',
      });
      const buffer = await blob.arrayBuffer();
      resolve({
        bytes: Array.from(new Uint8Array(buffer)),
        byteLength: buffer.byteLength,
        trackCount: cap.tracks.size,
        hadRecorder: true,
      });
    };

    try {
      recorder.requestData();
    } catch (e) { /* ignore */ }
    recorder.stop();
  });
})();
"""


async def install_audio_capture(context: BrowserContext) -> None:
    """Подключает перехват WebRTC-аудио до загрузки страницы встречи."""
    await context.add_init_script(AUDIO_CAPTURE_INIT_SCRIPT)


async def get_capture_status(page: Page) -> dict[str, Any]:
    """Возвращает статус in-page аудиорекордера для отладки."""
    try:
        status = await page.evaluate("() => window.__telemostAudioCaptureStatus?.()")
        return status or {}
    except Exception as exc:
        logger.debug("Не удалось получить статус аудиозахвата: %s", exc)
        return {}


async def stop_and_save_webrtc_audio(page: Page, output_webm: Path) -> Path | None:
    """
    Останавливает MediaRecorder на странице и сохраняет WebM с аудио.

    Returns:
        Путь к файлу или None, если дорожек не было.
    """
    try:
        result = await page.evaluate("() => window.__telemostStopAudioCapture()")
    except Exception as exc:
        logger.warning("Ошибка остановки WebRTC-записи: %s", exc)
        return None

    if not result:
        return None

    byte_length = int(result.get("byteLength") or 0)
    raw_bytes = result.get("bytes") or []

    logger.info(
        "WebRTC аудио: tracks=%s, recorder=%s, bytes=%d",
        result.get("trackCount"),
        result.get("hadRecorder"),
        byte_length,
    )

    if byte_length < 1000 or not raw_bytes:
        return None

    output_webm.parent.mkdir(parents=True, exist_ok=True)
    output_webm.write_bytes(bytes(raw_bytes))
    return output_webm
