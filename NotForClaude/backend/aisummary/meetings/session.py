"""In-memory live-meeting session manager.

Runs each meeting's capture+transcription loop on a background thread and
exposes a thread-safe queue of segments for the API/WebSocket to drain.
On stop, produces the SLM summary + PDF minutes.
"""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..config import Settings
from .transcribe import Segment, Transcript
from .streaming import run_live_meeting


@dataclass
class MeetingSession:
    id: str
    title: str
    stop_event: threading.Event
    seg_queue: "queue.Queue"
    thread: threading.Thread
    transcript: Optional[Transcript] = None
    done: bool = False


class MeetingManager:
    def __init__(self):
        self._sessions: dict[str, MeetingSession] = {}
        self._lock = threading.Lock()

    def start(self, settings: Settings, title: str, source: str = "mic",
              transcribe_fn=None, capture=None) -> str:
        if capture is None and source == "system":
            from .capture import SystemCapture
            capture = SystemCapture()
        stop_event = threading.Event()
        q: "queue.Queue" = queue.Queue()
        sid = f"m{int(threading.get_ident())}_{len(self._sessions)}"

        def on_segment(seg: Segment):
            q.put({"start": seg.start, "end": seg.end,
                   "text": seg.text, "speaker": seg.speaker})

        def run():
            tr = None
            try:
                tr = run_live_meeting(settings, title, on_segment, stop_event,
                                      capture=capture, transcribe_fn=transcribe_fn)
            except Exception:
                import traceback
                traceback.print_exc()
            with self._lock:
                sess = self._sessions.get(sid)
                if sess:
                    if tr is not None:
                        sess.transcript = tr
                    sess.done = True

        thread = threading.Thread(target=run, daemon=True)
        sess = MeetingSession(id=sid, title=title, stop_event=stop_event,
                              seg_queue=q, thread=thread)
        with self._lock:
            self._sessions[sid] = sess
        thread.start()
        return sid

    def poll(self, sid: str) -> list:
        sess = self._sessions.get(sid)
        if not sess:
            return []
        out = []
        while not sess.seg_queue.empty():
            out.append(sess.seg_queue.get())
        return out

    def is_done(self, sid: str) -> bool:
        sess = self._sessions.get(sid)
        return bool(sess and sess.done)

    def stop(self, sid: str, settings: Settings, provider) -> dict:
        sess = self._sessions.get(sid)
        if not sess:
            raise KeyError(sid)
        sess.stop_event.set()
        sess.thread.join(timeout=60)
        tr = sess.transcript or Transcript(meeting_id=sid, title=sess.title,
                                           started_at=0.0)
        import time as _t
        from ..config import MEETINGS_DIR
        from .minutes import (summarize, to_markdown, render_html, html_to_pdf,
                              render_pdf)
        from . import store
        MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
        when = _t.strftime("%Y-%m-%d %H:%M", _t.localtime(tr.started_at or _t.time()))

        # post-meeting speaker diarization (best-effort; needs pyannote + token)
        if getattr(settings, "diarize_enabled", False):
            try:
                from . import diarize as _diar
                wav = MEETINGS_DIR / f"{tr.meeting_id}.wav"
                if wav.exists():
                    turns = _diar.diarize_wav(wav)
                    if turns:
                        _diar.assign_speakers(tr.segments, turns)
            except Exception:
                import traceback
                traceback.print_exc()

        minutes = summarize(tr, settings, provider)
        md = to_markdown(minutes, tr, when)
        md_path = MEETINGS_DIR / f"{tr.meeting_id}.md"
        md_path.write_text(md, encoding="utf-8")

        pdf_path = MEETINGS_DIR / f"{tr.meeting_id}.pdf"
        try:
            html_to_pdf(render_html(minutes, tr, when), pdf_path)
        except Exception:
            pdf_path = render_pdf(minutes, tr)      # reportlab fallback

        store.save_record(tr.meeting_id, minutes.title or tr.title,
                          tr.started_at or _t.time(),
                          tr.to_dict(), minutes.__dict__, md_path, pdf_path)
        return {"meeting_id": tr.meeting_id, "minutes": minutes.__dict__,
                "markdown": md, "md_path": str(md_path), "pdf_path": str(pdf_path),
                "segments": len(tr.segments)}


# module-level singleton used by the API
MANAGER = MeetingManager()
