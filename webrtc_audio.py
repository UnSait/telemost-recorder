"""Захват звука встречи через WebRTC/MediaRecorder внутри страницы и фреймов."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Frame, Page

logger = logging.getLogger(__name__)

# Хуки на prototype + агрегация треков в top-window; stop/status всегда переопределяются
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

  window.__telemostStopAudioCapture = () => new Promise((resolve) => {
    const cap = getCap();
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
      const blob = new Blob(cap.chunks, { type: recorder.mimeType || 'audio/webm' });
      const buffer = await blob.arrayBuffer();
      resolve({
        bytes: Array.from(new Uint8Array(buffer)),
        byteLength: buffer.byteLength,
        trackCount: cap.tracks.size,
        hadRecorder: true,
      });
    };

    try { recorder.requestData(); } catch (e) { /* ignore */ }
    recorder.stop();
  });
})();
"""


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


async def stop_and_save_webrtc_audio(page: Page, output_webm: Path) -> Path | None:
    """
    Останавливает MediaRecorder и сохраняет WebM с аудио.

    Пробует все фреймы; выбирает самый большой результат.
    """
    await ensure_audio_capture(page)

    best_result: dict[str, Any] | None = None

    for frame in page.frames:
        try:
            result = await frame.evaluate(
                "() => window.__telemostStopAudioCapture?.()"
            )
            if not result:
                continue
            byte_length = int(result.get("byteLength") or 0)
            if byte_length > int((best_result or {}).get("byteLength") or 0):
                best_result = result
        except Exception as exc:
            logger.debug("Stop audio в фрейме %s: %s", frame.url[:60] if frame.url else frame, exc)

    if not best_result:
        logger.warning("WebRTC stop: нет результата ни в одном фрейме")
        return None

    byte_length = int(best_result.get("byteLength") or 0)
    raw_bytes = best_result.get("bytes") or []

    logger.info(
        "WebRTC аудио: tracks=%s, recorder=%s, bytes=%d",
        best_result.get("trackCount"),
        best_result.get("hadRecorder"),
        byte_length,
    )

    if byte_length < 1000 or not raw_bytes:
        return None

    output_webm.parent.mkdir(parents=True, exist_ok=True)
    output_webm.write_bytes(bytes(raw_bytes))
    return output_webm
