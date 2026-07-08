"""Bot lifecycle manager.

Each chat-app bot (Discord, Slack) runs in its own daemon thread with its own
event loop and forwards user questions to `answer_fn` (the RAG engine). The
manager keeps everything idempotent: `sync(settings, answer_fn)` starts the
bots that are enabled + configured and stops the ones that aren't. It is safe
to call on every settings change and at startup (auto-run).
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

AnswerFn = Callable[[str], str]


class _BotState:
    def __init__(self) -> None:
        self.thread: Optional[threading.Thread] = None
        self.token: str = ""
        self.status: str = "stopped"   # stopped | starting | connected | error
        self.detail: str = ""
        self.stop: Optional[Callable[[], None]] = None

    def running(self) -> bool:
        return self.status in ("starting", "connected")


class BotManager:
    def __init__(self) -> None:
        self._d = _BotState()
        self._s = _BotState()
        self._lock = threading.Lock()

    # ---- introspection ----
    def status(self) -> dict:
        return {
            "discord": {"status": self._d.status, "detail": self._d.detail,
                        "running": self._d.running()},
            "slack": {"status": self._s.status, "detail": self._s.detail,
                      "running": self._s.running()},
        }

    # ---- reconcile to settings (idempotent) ----
    def sync(self, settings, answer_fn: AnswerFn) -> None:
        with self._lock:
            self._sync_discord(settings, answer_fn)
            self._sync_slack(settings, answer_fn)

    def _sync_discord(self, settings, answer_fn: AnswerFn) -> None:
        token = (getattr(settings, "discord_bot_token", "") or "").strip()
        want = bool(getattr(settings, "discord_enabled", False)) and bool(token)
        if want and (not self._d.running() or self._d.token != token):
            self._stop_one(self._d)
            self._start(self._d, "discord", token, answer_fn,
                        lambda: _run_discord(token, answer_fn))
        elif not want and self._d.status != "stopped":
            self._stop_one(self._d)

    def _sync_slack(self, settings, answer_fn: AnswerFn) -> None:
        bt = (getattr(settings, "slack_bot_token", "") or "").strip()
        at = (getattr(settings, "slack_app_token", "") or "").strip()
        want = bool(getattr(settings, "slack_enabled", False)) and bool(bt) and bool(at)
        key = bt + "|" + at
        if want and (not self._s.running() or self._s.token != key):
            self._stop_one(self._s)
            self._start(self._s, "slack", key, answer_fn,
                        lambda: _run_slack(bt, at, answer_fn))
        elif not want and self._s.running():
            self._stop_one(self._s)

    # ---- generic start/stop ----
    def _start(self, st: _BotState, name: str, token: str,
               answer_fn: AnswerFn, runner) -> None:
        st.token = token
        st.status = "starting"
        st.detail = "연결 중…"

        def set_status(status: str, detail: str = "") -> None:
            st.status = status
            st.detail = detail

        def register_stop(fn) -> None:
            st.stop = fn

        def thread_body() -> None:
            try:
                runner_impl = runner()   # returns a callable(set_status, register_stop)
                runner_impl(set_status, register_stop)
            except ImportError as e:
                pkg = "discord.py" if name == "discord" else "slack_bolt"
                set_status("error", f"{pkg} 미설치 — pip install {pkg}  ({e})")
            except Exception as e:  # noqa: BLE001
                set_status("error", str(e)[:200])

        t = threading.Thread(target=thread_body, daemon=True, name=f"{name}-bot")
        st.thread = t
        t.start()

    def _stop_one(self, st: _BotState) -> None:
        if st.stop:
            try:
                st.stop()
            except Exception:
                pass
        st.stop = None
        st.status = "stopped"
        st.detail = ""
        st.token = ""

    def stop_all(self) -> None:
        with self._lock:
            self._stop_one(self._d)
            self._stop_one(self._s)


def _run_discord(token: str, answer_fn: AnswerFn):
    """Import lazily so a missing dependency is a clean 'not installed' status."""
    from .discord_bot import run_discord

    def impl(set_status, register_stop):
        run_discord(token, answer_fn, set_status, register_stop)
    return impl


def _run_slack(bot_token: str, app_token: str, answer_fn: AnswerFn):
    from .slack_bot import run_slack

    def impl(set_status, register_stop):
        run_slack(bot_token, app_token, answer_fn, set_status, register_stop)
    return impl


MANAGER = BotManager()
