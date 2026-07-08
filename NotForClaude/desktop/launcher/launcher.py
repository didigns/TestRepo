"""AISummary launcher.

The app entry point (shortcuts point here, run via pythonw for no console).
Sequence:
    1. Show a styled splash.
    2. Check Google Drive .meta for a newer version.
       - If found and user agrees: download the installer, run it, quit.
    3. Start the local backend (windowless) if not already running.
    4. Poll http://127.0.0.1:8756/health until ready.
    5. Launch the Tauri app window, then wait in the background and shut the
       backend down when the app closes.

Requires PySide6 (bundled in the app's private runtime). Pure-stdlib update
logic lives in aisummary_update.py.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QObject, QTimer
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLabel, QProgressBar,
    QGraphicsDropShadowEffect, QMessageBox,
)

import aisummary_update as upd

BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8756
BACKEND_URL = f"http://{BACKEND_HOST}:{BACKEND_PORT}"
HEALTH_URL = f"{BACKEND_URL}/health"
CREATE_NO_WINDOW = 0x08000000


# --------------------------------------------------------------------------
# Path / environment resolution (works installed and in the dev repo tree)
# --------------------------------------------------------------------------
BASE = Path(__file__).resolve().parent          # .../launcher
INSTALL = BASE.parent                           # install dir (or desktop/ in dev)


def _first_existing(paths):
    for p in paths:
        if p and Path(p).exists():
            return Path(p)
    return None


def data_dir() -> Path:
    d = Path(os.path.expanduser("~")) / ".aisummary"
    d.mkdir(parents=True, exist_ok=True)
    return d


def backend_dir():
    for cand in (INSTALL / "backend", INSTALL.parent / "backend"):
        if (cand / "aisummary").is_dir():
            return cand
    return None


def backend_python() -> str:
    # Prefer the app's private runtime; pythonw = no console window.
    cands = [
        INSTALL / "runtime" / "pythonw.exe",
        INSTALL / "runtime" / "python.exe",
        INSTALL / "runtime" / "bin" / "python",
    ]
    found = _first_existing(cands)
    if found:
        return str(found)
    # dev fallback: pythonw next to the current interpreter, else this one
    if os.name == "nt":
        w = Path(sys.executable).with_name("pythonw.exe")
        if w.exists():
            return str(w)
    return sys.executable


def tauri_exe():
    return _first_existing([
        INSTALL / "AISummary.exe",
        INSTALL / "app" / "AISummary.exe",
        INSTALL / "src-tauri" / "target" / "release" / "aisummary.exe",
        INSTALL / "src-tauri" / "target" / "debug" / "aisummary.exe",
    ])


def current_version() -> str:
    vf = INSTALL / "version.txt"
    if vf.exists():
        try:
            return vf.read_text(encoding="utf-8-sig").strip() or "0.0.0"
        except Exception:
            pass
    return "0.0.0"


def backend_up(timeout: float = 2.0) -> bool:
    try:
        urllib.request.urlopen(HEALTH_URL, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True  # any HTTP status means the server is listening
    except Exception:
        return False


# --------------------------------------------------------------------------
# Orchestration worker (runs off the GUI thread)
# --------------------------------------------------------------------------
class Worker(QObject):
    status = Signal(str)            # status text
    progress = Signal(int, int)     # downloaded, total (0 = indeterminate)
    ask_update = Signal(object)     # emit Meta -> main thread shows dialog
    done = Signal()                 # ready; app launched
    failed = Signal(str)            # fatal error message
    quit_app = Signal()             # request process exit

    def __init__(self):
        super().__init__()
        self._answer_event = threading.Event()
        self._answer = False
        self.backend_proc = None

    def set_update_answer(self, yes: bool):
        self._answer = yes
        self._answer_event.set()

    def run(self):
        try:
            self._run()
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))

    def _run(self):
        cur = current_version()

        # 1) Update check (non-fatal; AISUMMARY_NO_UPDATE=1 to skip)
        skip_update = os.environ.get("AISUMMARY_NO_UPDATE") == "1"
        self.status.emit("업데이트 확인 중…" if not skip_update else "시작 중…")
        try:
            if skip_update:
                raise RuntimeError("update check disabled")
            meta = upd.fetch_meta(timeout=15)
            if upd.is_newer(meta.version, cur):
                self._answer_event.clear()
                self.ask_update.emit(meta)
                self._answer_event.wait()
                if self._answer:
                    self._do_update(meta)
                    return
        except Exception as e:  # noqa: BLE001
            self._log(f"update check skipped: {e}")

        # 2) Start backend if needed
        if backend_up():
            self.status.emit("백엔드 연결됨")
        else:
            self.status.emit("백엔드 시작 중…")
            self._start_backend()

        # 3) Wait for readiness
        self.status.emit("서버 준비 중…")
        if not self._wait_backend(timeout=120):
            raise RuntimeError(
                "백엔드가 시간 내에 시작되지 않았습니다.\n"
                f"로그: {data_dir() / 'server.log'}"
            )

        # 4) Launch the app window
        exe = tauri_exe()
        if not exe:
            raise RuntimeError("앱 실행 파일(AISummary.exe)을 찾을 수 없습니다.")
        self.status.emit("앱 여는 중…")
        env = dict(os.environ)
        env["AISUMMARY_NO_UPDATE"] = "1"   # launcher owns updates
        env["AISUMMARY_NO_BACKEND"] = "1"  # launcher owns the backend
        app_proc = subprocess.Popen([str(exe)], env=env)
        self.done.emit()

        # 5) Supervise on a plain daemon thread so this QThread's event loop
        # stays free (blocking here would freeze queued signals). When the app
        # window closes, stop the backend and ask the process to exit.
        def _supervise():
            app_proc.wait()
            self._stop_backend()
            self.quit_app.emit()

        threading.Thread(target=_supervise, daemon=True).start()

    # -- update -----------------------------------------------------------
    def _do_update(self, meta):
        self.status.emit(f"새 버전 v{meta.version} 다운로드 중…")
        path = upd.download_installer(
            meta.installer,
            dest_dir=str(data_dir() / "updates"),
            progress=lambda d, t: self.progress.emit(d, t),
        )
        self.status.emit("설치 프로그램 실행 중…")
        subprocess.Popen([path])
        time.sleep(0.8)
        self.quit_app.emit()

    # -- backend ----------------------------------------------------------
    def _start_backend(self):
        bdir = backend_dir()
        if not bdir:
            raise RuntimeError("backend 폴더(aisummary)를 찾을 수 없습니다.")
        log = open(data_dir() / "server.log", "a", encoding="utf-8", buffering=1)
        log.write(f"\n[launcher] start backend @ {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = CREATE_NO_WINDOW
        self.backend_proc = subprocess.Popen(
            [backend_python(), "-m", "aisummary.api"],
            cwd=str(bdir), stdout=log, stderr=subprocess.STDOUT, **kwargs,
        )

    def _stop_backend(self):
        if self.backend_proc and self.backend_proc.poll() is None:
            try:
                self.backend_proc.terminate()
            except Exception:  # noqa: BLE001
                pass

    def _wait_backend(self, timeout: int) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if backend_up(timeout=2.0):
                return True
            if self.backend_proc and self.backend_proc.poll() is not None:
                return False  # our backend child died
            time.sleep(0.5)
        return False

    def _log(self, msg: str):
        try:
            with open(data_dir() / "launcher.log", "a", encoding="utf-8") as f:
                f.write(f"[launcher] {msg}\n")
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# Splash UI
# --------------------------------------------------------------------------
ACCENT = "#4f8cff"
CARD = "#20242b"
FG = "#e8ebf0"
MUTED = "#8b93a1"

STYLE = f"""
#card {{ background: {CARD}; border-radius: 18px; }}
QLabel#title {{ color: {FG}; font-size: 22px; font-weight: 700; }}
QLabel#subtitle {{ color: {MUTED}; font-size: 12px; }}
QLabel#status {{ color: {FG}; font-size: 13px; }}
QProgressBar {{
    background: #2b3038; border: none; border-radius: 5px;
    height: 8px; text-align: center; color: transparent;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}
"""


class Splash(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.SplashScreen
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(460, 300)

        card = QWidget(self)
        card.setObjectName("card")
        card.setGeometry(20, 20, 420, 260)
        shadow = QGraphicsDropShadowEffect(blurRadius=40, xOffset=0, yOffset=8)
        shadow.setColor(QColor(0, 0, 0, 160))
        card.setGraphicsEffect(shadow)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(36, 34, 36, 30)
        lay.setSpacing(10)

        logo = QLabel()
        logo.setAlignment(Qt.AlignCenter)
        pix = self._logo_pixmap()
        if pix:
            logo.setPixmap(pix)
        lay.addWidget(logo)

        title = QLabel("AISummary")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignCenter)
        lay.addWidget(title)

        sub = QLabel("100% 로컬 · 프라이버시 우선")
        sub.setObjectName("subtitle")
        sub.setAlignment(Qt.AlignCenter)
        lay.addWidget(sub)

        lay.addStretch(1)

        self.bar = QProgressBar()
        self.bar.setRange(0, 0)  # indeterminate by default
        self.bar.setTextVisible(False)
        lay.addWidget(self.bar)

        self.status = QLabel("시작 중…")
        self.status.setObjectName("status")
        self.status.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.status)

        self.setStyleSheet(STYLE)
        self._center()

    def _logo_pixmap(self):
        for name in ("icon.png", "128x128.png"):
            for base in (BASE / "assets", INSTALL / "icons",
                         INSTALL / "src-tauri" / "icons"):
                p = base / name
                if p.exists():
                    return QPixmap(str(p)).scaled(
                        72, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation
                    )
        return None

    def _center(self):
        scr = QApplication.primaryScreen().geometry()
        self.move(
            scr.center().x() - self.width() // 2,
            scr.center().y() - self.height() // 2,
        )

    def set_status(self, text: str):
        self.status.setText(text)

    def set_progress(self, done: int, total: int):
        if total > 0:
            self.bar.setRange(0, total)
            self.bar.setValue(done)
        else:
            self.bar.setRange(0, 0)


# --------------------------------------------------------------------------
class Controller(QObject):
    """Lives on the main (GUI) thread. Worker signals connect here so their
    slots run on the main thread (queued), not on the worker thread."""

    def __init__(self, app, splash, worker):
        super().__init__()
        self.app = app
        self.splash = splash
        self.worker = worker

    def on_ask_update(self, meta):
        box = QMessageBox(self.splash)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("업데이트")
        box.setText(f"새 버전 v{meta.version}이(가) 있습니다 (현재 v{current_version()}).")
        box.setInformativeText((meta.notes or "") + "\n\n지금 설치할까요?")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.Yes)
        yes = box.exec() == QMessageBox.Yes
        self.worker.set_update_answer(yes)

    def on_done(self):
        # App window launched — close the splash shortly after.
        QTimer.singleShot(500, self.splash.close)

    def on_failed(self, msg: str):
        self.splash.hide()
        QMessageBox.critical(None, "AISummary", f"시작 실패:\n{msg}")
        self.app.quit()

    def on_quit(self):
        self.app.quit()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    splash = Splash()
    splash.show()

    thread = QThread()
    worker = Worker()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)

    ctrl = Controller(app, splash, worker)
    worker.status.connect(splash.set_status)
    worker.progress.connect(splash.set_progress)
    worker.ask_update.connect(ctrl.on_ask_update)
    worker.done.connect(ctrl.on_done)
    worker.failed.connect(ctrl.on_failed)
    worker.quit_app.connect(ctrl.on_quit)

    thread.start()
    rc = app.exec()
    thread.quit()
    thread.wait(2000)
    sys.exit(rc)


if __name__ == "__main__":
    main()
