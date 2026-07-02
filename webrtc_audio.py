"""Захват звука встречи через WebRTC/MediaRecorder внутри страницы и фреймов."""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Frame, Page

logger = logging.getLogger(__name__)

# Чанки сбрасываются на диск из Python, а не копятся в RAM до конца встречи
AUDIO_CAPTURE_INIT_SCRIPT = """
(() => {
  const installHooksOnce = () => {
    if (window.__telemostHooksInstalled) return;
    window.__telemostHooksInstalled = true;

    const forwardTrack = (track) => {
      if (!track || track.kind !== 'audio') return;
      try {
        if (window.top && window.top !== window && window.top.__telemostRegisterTrack) {
          window.top.__telemostRegisterTrack(track);
          return;
        }
      } catch (e) { /* cross-origin iframe */ }
      if (window.__telemostRegisterTrack) {
        window.__telemostRegisterTrack(track);
      }
    };

    const hookPC = (PC) => {
      if (!PC || !PC.prototype) return;
      const proto = PC.prototype;

      const afterConnect = (pc) => {
        pc.addEventListener('track', (ev) => forwardTrack(ev.track));
        try {
          pc.getReceivers().forEach((r) => forwardTrack(r.track));
        } catch (e) { /* ignore */ }
      };

      const OrigCtor = PC;
      const Wrapped = function (...args) {
        const pc = new OrigCtor(...args);
        afterConnect(pc);
        return pc;
      };
      Wrapped.prototype = OrigCtor.prototype;
      if (window.RTCPeerConnection === OrigCtor) window.RTCPeerConnection = Wrapped;
      if (window.webkitRTCPeerConnection === OrigCtor) window.webkitRTCPeerConnection = Wrapped;

      if (proto.setRemoteDescription) {
        const orig = proto.setRemoteDescription;
        proto.setRemoteDescription = async function (...args) {
          const result = await orig.apply(this, args);
          try {
            this.getReceivers().forEach((r) => forwardTrack(r.track));
          } catch (e) { /* ignore */ }
          return result;
        };
      }
    };

    hookPC(window.RTCPeerConnection);
    hookPC(window.webkitRTCPeerConnection);

    const hookMediaElement = (element) => {
      if (!element || element.__telemostHooked) return;
      element.__telemostHooked = true;
      const capture = () => {
        const stream = element.srcObject;
        if (stream && stream.getAudioTracks) {
          stream.getAudioTracks().forEach(forwardTrack);
        }
      };
      element.addEventListener('loadedmetadata', capture);
      element.addEventListener('play', capture);
      capture();
    };

    const scan = () => document.querySelectorAll('video,audio').forEach(hookMediaElement);
    new MutationObserver(scan).observe(document.documentElement, { childList: true, subtree: true });
    scan();
  };

  const getCap = () => {
    try {
      if (window.top && window.top.__telemostAudioCapture) {
        return window.top.__telemostAudioCapture;
      }
    } catch (e) { /* ignore */ }
    if (!window.__telemostAudioCapture) {
      window.__telemostAudioCapture = { chunks: [], tracks: new Map(), recorder: null };
    }
    return window.__telemostAudioCapture;
  };

  const maybeStartRecorder = () => {
    const cap = getCap();
    if (cap.recorder && cap.recorder.state === 'recording') return;

    const tracks = [...cap.tracks.values()].filter((t) => t && t.readyState === 'live');
    if (!tracks.length) return;

    const stream = new MediaStream(tracks);
    const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
      ? 'audio/webm;codecs=opus'
      : 'audio/webm';

    const recorder = new MediaRecorder(stream, { mimeType: mime, audioBitsPerSecond: 128000 });
    cap.chunks = [];
    recorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) cap.chunks.push(event.data);
    };
    recorder.start(1000);
    cap.recorder = recorder;
  };

  window.__telemostRegisterTrack = (track) => {
    if (!track || track.kind !== 'audio') return;
    const cap = getCap();
    cap.tracks.set(track.id, track);
    track.addEventListener('ended', () => cap.tracks.delete(track.id));
    maybeStartRecorder();
  };

  installHooksOnce();
  getCap();

  window.__telemostAudioCaptureStatus = () => {
    const cap = getCap();
    return {
      installed: !!window.__telemostHooksInstalled,
      trackCount: cap.tracks.size,
      recorderState: cap.recorder ? cap.recorder.state : 'none',
      chunkCount: cap.chunks.length,
      frame: window.location.href,
    };
  };

  window.__telemostDrainAudioChunks = async () => {
    const cap = getCap();
    if (!cap.chunks.length) {
      return { b64: '', byteLength: 0, chunkCount: 0 };
    }

    const chunks = cap.chunks.splice(0);
    const mime = (cap.recorder && cap.recorder.mimeType) || 'audio/webm';
    const blob = new Blob(chunks, { type: mime });
    const buffer = await blob.arrayBuffer();
    const bytes = new Uint8Array(buffer);

    let binary = '';
    for (let i = 0; i < bytes.length; i += 1) {
      binary += String.fromCharCode(bytes[i]);
    }

    return {
      b64: btoa(binary),
      byteLength: bytes.length,
      chunkCount: chunks.length,
    };
  };

  window.__telemostStopAudioCapture = () => new Promise((resolve) => {
    const cap = getCap();
    const recorder = cap.recorder;

    if (!recorder || recorder.state === 'inactive') {
      resolve({
        stopped: false,
        trackCount: cap.tracks.size,
        hadRecorder: false,
      });
      return;
    }

    recorder.onstop = () => {
      resolve({
        stopped: true,
        trackCount: cap.tracks.size,
        hadRecorder: true,
      });
    };

    try { recorder.requestData(); } catch (e) { /* ignore */ }
    recorder.stop();
    cap.recorder = null;
  });
})();
"""

CHUNK_FLUSH_INTERVAL_SEC = 5
MIN_WEBM_BYTES = 1000


async def install_audio_capture(context: BrowserContext) -> None:
    """Подключает перехват WebRTC-аудио до загрузки страницы встречи."""
    await context.add_init_script(AUDIO_CAPTURE_INIT_SCRIPT)


async def _inject_frame(frame: Frame) -> bool:
    """Внедряет скрипт в один фрейм. Возвращает False при cross-origin."""
    try:
        await frame.evaluate(AUDIO_CAPTURE_INIT_SCRIPT)
        return True
    except Exception as exc:
        logger.debug("Пропуск фрейма %s: %s", frame.url[:80] if frame.url else frame, exc)
        return False


async def ensure_audio_capture(page: Page) -> int:
    """Внедряет скрипт в главную страницу и все доступные фреймы."""
    injected = 0
    if await _inject_frame(page.main_frame):
        injected += 1
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        if await _inject_frame(frame):
            injected += 1
    return injected


async def _get_best_capture_frame(page: Page) -> Frame:
    """Фрейм с наибольшим числом треков/чанков — там живёт основной рекордер."""
    best_frame = page.main_frame
    best_score = -1

    for frame in page.frames:
        try:
            status = await frame.evaluate(
                "() => window.__telemostAudioCaptureStatus?.() || null"
            )
            if not status:
                continue
            score = int(status.get("chunkCount", 0)) * 1000 + int(status.get("trackCount", 0))
            if score > best_score:
                best_score = score
                best_frame = frame
        except Exception:
            continue

    return best_frame


async def get_capture_status(page: Page) -> dict[str, Any]:
    """Возвращает статус in-page аудиорекордера для отладки."""
    best: dict[str, Any] = {}
    for frame in page.frames:
        try:
            status = await frame.evaluate(
                "() => window.__telemostAudioCaptureStatus?.() || null"
            )
            if not status:
                continue
            if status.get("trackCount", 0) > best.get("trackCount", 0):
                best = {**status, "frame": frame.url}
        except Exception:
            continue
    return best


async def flush_audio_chunks_to_file(page: Page, output_webm: Path) -> int:
    """
    Забирает накопленные чанки из браузера и дописывает их в WebM на диск.

    Освобождает RAM в renderer-процессе Chromium (cap.chunks очищается).

    Returns:
        Число записанных байт (0, если чанков не было).
    """
    frame = await _get_best_capture_frame(page)

    try:
        result = await frame.evaluate("() => window.__telemostDrainAudioChunks?.()")
    except Exception as exc:
        logger.debug("Drain audio chunks: %s", exc)
        return 0

    if not result:
        return 0

    b64 = result.get("b64") or ""
    if not b64:
        return 0

    data = base64.b64decode(b64)
    if not data:
        return 0

    output_webm.parent.mkdir(parents=True, exist_ok=True)
    with output_webm.open("ab") as handle:
        handle.write(data)

    nbytes = len(data)
    logger.debug(
        "Сброшено %d байт (%d чанков) → %s",
        nbytes,
        result.get("chunkCount", 0),
        output_webm,
    )
    return nbytes


async def stop_webrtc_recorder(page: Page) -> dict[str, Any]:
    """Останавливает MediaRecorder без передачи всего файла через Playwright."""
    for frame in page.frames:
        try:
            result = await frame.evaluate("() => window.__telemostStopAudioCapture?.()")
            if result and result.get("hadRecorder"):
                return result
        except Exception as exc:
            logger.debug("Stop recorder в фрейме %s: %s", frame.url[:60] if frame.url else frame, exc)

    return {"stopped": False, "hadRecorder": False, "trackCount": 0}


async def finalize_webrtc_audio(page: Page, output_webm: Path) -> Path | None:
    """
    Останавливает рекордер, сбрасывает оставшиеся чанки на диск.

    Файл собирается инкрементально; в Python не держится весь WebM в памяти.
    """
    await ensure_audio_capture(page)

    stop_info = await stop_webrtc_recorder(page)
    await asyncio.sleep(0.3)

    await flush_audio_chunks_to_file(page, output_webm)
    await flush_audio_chunks_to_file(page, output_webm)

    if not output_webm.exists():
        logger.warning(
            "WebRTC finalize: файл не создан (hadRecorder=%s)",
            stop_info.get("hadRecorder"),
        )
        return None

    size = output_webm.stat().st_size
    logger.info(
        "WebRTC аудио на диске: %s (%d байт, tracks=%s, hadRecorder=%s)",
        output_webm,
        size,
        stop_info.get("trackCount"),
        stop_info.get("hadRecorder"),
    )

    if size < MIN_WEBM_BYTES:
        return None

    return output_webm
