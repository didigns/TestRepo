"""Slack bridge via Socket Mode (no public URL required).

Setup:
  1. pip install slack_bolt
  2. Create a Slack app (from scratch). Enable Socket Mode.
  3. Bot Token Scopes: app_mentions:read, chat:write, im:history, im:read.
     Install to workspace -> copy the BOT TOKEN (xoxb-...).
  4. Create an App-Level Token with connections:write (xapp-...).
  5. Event Subscriptions -> subscribe to: app_mention, message.im.

The bot replies when @mentioned in a channel, or on any direct message.
"""
from __future__ import annotations

import re


def run_slack(bot_token, app_token, answer_fn, set_status, register_stop) -> None:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    app = App(token=bot_token)

    def _answer(text: str):
        q = re.sub(r"<@[^>]+>", "", text or "").strip()
        if not q:
            return None
        try:
            return answer_fn(q)
        except Exception as e:  # noqa: BLE001
            return f"오류: {e}"

    @app.event("app_mention")
    def _on_mention(event, say):
        a = _answer(event.get("text", ""))
        if a:
            say(a)

    @app.event("message")
    def _on_message(event, say):
        # only respond to direct messages from real users
        if (event.get("channel_type") == "im"
                and not event.get("bot_id")
                and not event.get("subtype")):
            a = _answer(event.get("text", ""))
            if a:
                say(a)

    handler = SocketModeHandler(app, app_token)

    def _stop():
        try:
            handler.close()
        except Exception:
            pass
    register_stop(_stop)

    set_status("connected", "Slack 소켓 모드 연결됨")
    handler.start()   # blocks until closed
    set_status("stopped", "")
