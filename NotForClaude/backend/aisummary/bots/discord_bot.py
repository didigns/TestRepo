"""Discord bridge — answers via a /ask slash command, @mentions, and DMs.

Setup:
  1. pip install discord.py
  2. Discord Developer Portal → 새 Application → Bot 생성 → BOT TOKEN 복사 (설정에 입력).
  3. Bot 탭에서 "MESSAGE CONTENT INTENT" 켜기 (privileged) — 멘션/DM 텍스트를 읽는 데 필요.
  4. 봇을 서버에 초대 (반드시 `bot` + `applications.commands` 스코프 포함):
       https://discord.com/api/oauth2/authorize?client_id=<APP_ID>&permissions=68608&scope=bot%20applications.commands
     (권한 68608 = 채널 보기 + 메시지 보내기 + 메시지 기록 읽기)

사용법: 서버 채널에서 `/ask 질문` , 봇을 @멘션, 또는 봇에게 DM.
"""
from __future__ import annotations

import asyncio


def _chunks(s: str, n: int):
    s = s or "(빈 응답)"
    return [s[i:i + n] for i in range(0, len(s), n)] or [s]


def run_discord(token, answer_fn, set_status, register_stop) -> None:
    import discord
    from discord import app_commands

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    # ---- /ask slash command ----
    @tree.command(name="ask", description="내 문서를 근거로 질문에 답합니다")
    @app_commands.describe(question="질문 내용")
    async def ask_cmd(interaction, question: str):
        await interaction.response.defer(thinking=True)
        try:
            answer = await asyncio.to_thread(answer_fn, question)
        except Exception as e:
            answer = f"오류: {e}"
        parts = _chunks(answer, 1900)
        await interaction.followup.send(parts[0])
        for extra in parts[1:]:
            await interaction.followup.send(extra)

    @client.event
    async def on_ready():
        # Register the slash command. Per-guild sync is instant; global sync is
        # a fallback (can take up to ~1h to appear).
        try:
            for g in list(client.guilds):
                try:
                    tree.copy_global_to(guild=g)
                    await tree.sync(guild=g)
                except Exception:
                    pass
            await tree.sync()
        except Exception:
            pass
        try:
            set_status("connected", f"{client.user} 로 로그인됨 · /ask 사용 가능")
        except Exception:
            pass

    @client.event
    async def on_message(message):
        try:
            if client.user is None or message.author.id == client.user.id:
                return
            is_dm = isinstance(message.channel, discord.DMChannel)
            mentioned = client.user in getattr(message, "mentions", [])
            if not (is_dm or mentioned):
                return
            q = message.content or ""
            for m in getattr(message, "mentions", []):
                q = q.replace(f"<@{m.id}>", "").replace(f"<@!{m.id}>", "")
            q = q.strip()
            if not q:
                return
            async with message.channel.typing():
                answer = await asyncio.to_thread(answer_fn, q)
            for chunk in _chunks(answer, 1900):
                await message.channel.send(chunk)
        except Exception as e:  # never let a handler crash the loop
            try:
                await message.channel.send(f"오류: {e}")
            except Exception:
                pass

    # Allow the manager (another thread) to stop this bot gracefully.
    def _stop():
        try:
            fut = asyncio.run_coroutine_threadsafe(client.close(), client.loop)
            fut.result(timeout=5)
        except Exception:
            pass
    register_stop(_stop)

    client.run(token, log_handler=None)
    set_status("stopped", "")
