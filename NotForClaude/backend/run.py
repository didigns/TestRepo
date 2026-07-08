"""AISummary server supervisor.

Runs the local server and automatically restarts it if it crashes (native
library segfaults in llama.cpp / ctranslate2 / soundcard can take the whole
process down — this keeps the app alive). Crash output is captured to
~/.aisummary/server.log so failures can be diagnosed.

Use this instead of `python -m aisummary.api`:

    cd backend
    python run.py
"""
from __future__ import annotations

import datetime
import subprocess
import sys
import time
from pathlib import Path

LOG = Path.home() / ".aisummary" / "server.log"


def main() -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    print(f"[supervisor] AISummary — auto-restart on. 로그: {LOG}")
    backoff = 2
    while True:
        started = time.time()
        with open(LOG, "a", encoding="utf-8") as log:
            log.write(f"\n[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] "
                      f"===== server start =====\n")
            log.flush()
            # capture stdout+stderr to the log so a crash traceback is kept
            proc = subprocess.Popen([sys.executable, "-m", "aisummary.api"],
                                    stdout=log, stderr=subprocess.STDOUT)
            try:
                code = proc.wait()
            except KeyboardInterrupt:
                proc.terminate()
                print("\n[supervisor] 종료합니다.")
                return
            log.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] "
                      f"server exited (code={code})\n")
        ran = time.time() - started
        # reset backoff if it ran a while; otherwise grow it (crash loop guard)
        backoff = 2 if ran > 30 else min(backoff * 2, 30)
        print(f"[supervisor] 서버가 종료됨 (code={code}, {ran:.0f}s 실행). "
              f"{backoff}s 후 재시작… (원인은 {LOG} 확인)")
        time.sleep(backoff)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
