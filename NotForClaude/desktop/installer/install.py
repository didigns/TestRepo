"""OwnYourPC — Qt (PySide6) install wizard.

Run by the setup.exe bootstrapper on the app's private runtime:
    runtime\\pythonw.exe installer\\install.py --payload <extracted_dir>

The <payload> dir holds the already-extracted app files:
    runtime/  backend/  frontend/  icons/  launcher/  OwnYourPC.exe
    installer/install.py  installer/uninstall.py  version.txt

The wizard copies those into the install location, creates shortcuts that
launch the launcher, and registers an uninstaller. No pip at install time —
the runtime already contains every dependency.

Standalone test (no real runtime needed to see the UI):
    python install.py --payload <some_dir>
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QObject
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QStackedWidget, QLineEdit, QFileDialog, QCheckBox, QProgressBar,
    QGraphicsDropShadowEffect, QFrame, QMessageBox,
)

APP_NAME = "OwnYourPC"
PUBLISHER = "OwnYourPC"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# ---- theme ---------------------------------------------------------------
ACCENT = "#4f8cff"
BG = "#191b1f"
CARD = "#20242b"
SIDE = "#161a20"
FG = "#e8ebf0"
MUTED = "#8b93a1"
FIELD = "#2b3038"

STYLE = f"""
#root {{ background: {BG}; }}
#side {{ background: {SIDE}; border-top-left-radius: 14px; border-bottom-left-radius: 14px; }}
#content {{ background: {BG}; border-top-right-radius: 14px; border-bottom-right-radius: 14px; }}
QLabel {{ color: {FG}; }}
QLabel#h1 {{ font-size: 21px; font-weight: 700; }}
QLabel#h2 {{ font-size: 15px; font-weight: 600; }}
QLabel#muted {{ color: {MUTED}; font-size: 12px; }}
QLabel#step {{ color: {MUTED}; font-size: 13px; padding: 6px 0; }}
QLabel#stepActive {{ color: {FG}; font-size: 13px; font-weight: 700; padding: 6px 0; }}
QLineEdit {{ background: {FIELD}; border: 1px solid #333a44; border-radius: 8px;
             padding: 8px 10px; color: {FG}; }}
QCheckBox {{ color: {FG}; spacing: 8px; }}
QProgressBar {{ background: {FIELD}; border: none; border-radius: 5px; height: 10px;
                text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}
QPushButton {{ background: {FIELD}; color: {FG}; border: none; border-radius: 8px;
               padding: 9px 20px; font-size: 13px; }}
QPushButton:hover {{ background: #333b46; }}
QPushButton#primary {{ background: {ACCENT}; color: white; font-weight: 600; }}
QPushButton#primary:hover {{ background: #5f98ff; }}
QPushButton:disabled {{ color: #5b6472; background: #262b33; }}
QTextEdit, #logbox {{ background: {CARD}; color: {MUTED}; border-radius: 8px; }}
"""


def default_install_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return str(Path(base) / "Programs" / APP_NAME)


# --------------------------------------------------------------------------
# Install worker
# --------------------------------------------------------------------------
class Installer(QObject):
    progress = Signal(int, int)     # done, total
    log = Signal(str)
    finished = Signal()
    failed = Signal(str)

    def __init__(self, payload: Path, dest: Path, desktop_shortcut: bool):
        super().__init__()
        self.payload = payload
        self.dest = dest
        self.desktop_shortcut = desktop_shortcut
        self.version = self._read_version()

    def _read_version(self) -> str:
        vf = self.payload / "version.txt"
        if vf.exists():
            try:
                return vf.read_text(encoding="utf-8-sig").strip() or "0.0.0"
            except Exception:
                pass
        return "0.0.0"

    def run(self):
        try:
            self._install()
            self.finished.emit()
        except Exception:  # noqa: BLE001
            self.failed.emit(traceback.format_exc())

    def _install(self):
        self._stop_running()
        self.dest.mkdir(parents=True, exist_ok=True)

        files = [p for p in self.payload.rglob("*") if p.is_file()]
        total = len(files) or 1
        self.log.emit(f"{total}개 파일 복사 중 → {self.dest}")
        for i, src in enumerate(files, 1):
            rel = src.relative_to(self.payload)
            out = self.dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)
            if i % 25 == 0 or i == total:
                self.progress.emit(i, total)

        (self.dest / "version.txt").write_text(self.version, encoding="utf-8")

        self.log.emit("바로가기 생성 중…")
        self._make_shortcuts()

        self.log.emit("제거 프로그램 등록 중…")
        self._register_uninstall()

        self.log.emit(f"설치 완료: {APP_NAME} v{self.version}")

    def _stop_running(self):
        if os.name != "nt":
            return
        for name in ("OwnYourPC.exe", "pythonw.exe"):
            try:
                subprocess.run(["taskkill", "/F", "/IM", name, "/FI",
                                f"WINDOWTITLE eq {APP_NAME}*"],
                               creationflags=CREATE_NO_WINDOW,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:  # noqa: BLE001
                pass

    def _launcher_target(self):
        pyw = self.dest / "runtime" / "pythonw.exe"
        launcher = self.dest / "launcher" / "launcher.py"
        icon = self.dest / "icons" / "icon.ico"
        return str(pyw), f'"{launcher}"', str(icon if icon.exists() else pyw)

    def _make_shortcuts(self):
        if os.name != "nt":
            self.log.emit("(비-Windows: 바로가기 생략)")
            return
        target, args, icon = self._launcher_target()
        workdir = str(self.dest)

        def make(lnk: str):
            ps = (
                "$w=New-Object -ComObject WScript.Shell;"
                f"$s=$w.CreateShortcut('{lnk}');"
                f"$s.TargetPath='{target}';"
                f"$s.Arguments='{args}';"
                f"$s.WorkingDirectory='{workdir}';"
                f"$s.IconLocation='{icon}';"
                f"$s.Description='OwnYourPC — 로컬 문서 RAG + 회의록';"
                "$s.Save()"
            )
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-Command", ps], creationflags=CREATE_NO_WINDOW,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        appdata = os.environ.get("APPDATA", "")
        start_dir = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / APP_NAME
        start_dir.mkdir(parents=True, exist_ok=True)
        make(str(start_dir / f"{APP_NAME}.lnk"))

        if self.desktop_shortcut:
            desktop = Path(os.path.expanduser("~")) / "Desktop"
            if desktop.exists():
                make(str(desktop / f"{APP_NAME}.lnk"))

    def _register_uninstall(self):
        if os.name != "nt":
            return
        import winreg
        pyw = self.dest / "runtime" / "pythonw.exe"
        uninst = self.dest / "installer" / "uninstall.py"
        icon = self.dest / "icons" / "icon.ico"
        size_kb = int(sum(p.stat().st_size for p in self.dest.rglob("*") if p.is_file()) / 1024)
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\\" + APP_NAME
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            def s(name, val):
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, str(val))

            def d(name, val):
                winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, int(val))

            s("DisplayName", APP_NAME)
            s("DisplayVersion", self.version)
            s("Publisher", PUBLISHER)
            s("DisplayIcon", icon if icon.exists() else pyw)
            s("InstallLocation", self.dest)
            s("UninstallString", f'"{pyw}" "{uninst}"')
            d("NoModify", 1)
            d("NoRepair", 1)
            d("EstimatedSize", size_kb)


# --------------------------------------------------------------------------
# Wizard UI
# --------------------------------------------------------------------------
class Wizard(QWidget):
    STEPS = ["시작", "옵션", "설치", "완료"]

    def __init__(self, payload: Path):
        super().__init__()
        self.payload = payload
        self.version = self._peek_version()
        self.setWindowTitle(f"{APP_NAME} 설치")
        self.setFixedSize(660, 460)
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._drag = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        root = QFrame()
        root.setObjectName("root")
        sh = QGraphicsDropShadowEffect(blurRadius=44, xOffset=0, yOffset=10)
        sh.setColor(QColor(0, 0, 0, 170))
        root.setGraphicsEffect(sh)
        outer.addWidget(root)

        row = QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        # sidebar
        side = QFrame()
        side.setObjectName("side")
        side.setFixedWidth(200)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(24, 30, 20, 24)
        sl.setSpacing(4)
        logo = QLabel()
        logo.setAlignment(Qt.AlignLeft)
        pm = self._logo()
        if pm:
            logo.setPixmap(pm)
        sl.addWidget(logo)
        name = QLabel(APP_NAME)
        name.setObjectName("h2")
        sl.addWidget(name)
        sub = QLabel("설치 마법사")
        sub.setObjectName("muted")
        sl.addWidget(sub)
        sl.addSpacing(24)
        self.step_labels = []
        for st in self.STEPS:
            lb = QLabel("○  " + st)
            lb.setObjectName("step")
            sl.addWidget(lb)
            self.step_labels.append(lb)
        sl.addStretch(1)
        ver = QLabel(f"v{self.version}")
        ver.setObjectName("muted")
        sl.addWidget(ver)
        row.addWidget(side)

        # content
        content = QFrame()
        content.setObjectName("content")
        cl = QVBoxLayout(content)
        cl.setContentsMargins(34, 30, 34, 24)
        self.stack = QStackedWidget()
        cl.addWidget(self.stack, 1)

        self.stack.addWidget(self._page_welcome())
        self.stack.addWidget(self._page_options())
        self.stack.addWidget(self._page_progress())
        self.stack.addWidget(self._page_done())

        # buttons
        btns = QHBoxLayout()
        btns.addStretch(1)
        self.btn_cancel = QPushButton("취소")
        self.btn_cancel.clicked.connect(self.close)
        self.btn_back = QPushButton("뒤로")
        self.btn_back.clicked.connect(self._back)
        self.btn_next = QPushButton("다음")
        self.btn_next.setObjectName("primary")
        self.btn_next.clicked.connect(self._next)
        btns.addWidget(self.btn_cancel)
        btns.addWidget(self.btn_back)
        btns.addWidget(self.btn_next)
        cl.addLayout(btns)
        row.addWidget(content, 1)

        self.setStyleSheet(STYLE)
        self._sync(0)
        self._center()

    # -- pages ------------------------------------------------------------
    def _page_welcome(self):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setSpacing(10)
        h = QLabel(f"{APP_NAME} 설치를 시작합니다")
        h.setObjectName("h1")
        l.addWidget(h)
        p = QLabel(
            "100% 로컬로 동작하는 문서 RAG Q&A와 실시간 회의 전사·회의록 앱입니다.\n"
            "클라우드 전송 없이, 이 PC 안에서만 동작합니다.\n\n"
            "Python 런타임이 함께 포함되어 별도 설치가 필요 없습니다."
        )
        p.setObjectName("muted")
        p.setWordWrap(True)
        l.addWidget(p)
        l.addStretch(1)
        return w

    def _page_options(self):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setSpacing(10)
        h = QLabel("설치 옵션")
        h.setObjectName("h1")
        l.addWidget(h)
        l.addSpacing(6)
        l.addWidget(QLabel("설치 위치"))
        row = QHBoxLayout()
        self.path_edit = QLineEdit(default_install_dir())
        browse = QPushButton("찾아보기")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path_edit, 1)
        row.addWidget(browse)
        l.addLayout(row)
        l.addSpacing(10)
        self.cb_desktop = QCheckBox("바탕화면에 바로가기 만들기")
        self.cb_desktop.setChecked(True)
        l.addWidget(self.cb_desktop)
        self.cb_launch = QCheckBox("설치 후 실행")
        self.cb_launch.setChecked(True)
        l.addWidget(self.cb_launch)
        l.addStretch(1)
        return w

    def _page_progress(self):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setSpacing(12)
        h = QLabel("설치 중…")
        h.setObjectName("h1")
        l.addWidget(h)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        l.addWidget(self.bar)
        self.log_label = QLabel("준비 중…")
        self.log_label.setObjectName("muted")
        self.log_label.setWordWrap(True)
        l.addWidget(self.log_label)
        l.addStretch(1)
        return w

    def _page_done(self):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setSpacing(10)
        h = QLabel("설치가 완료되었습니다")
        h.setObjectName("h1")
        l.addWidget(h)
        p = QLabel("시작 메뉴 또는 바탕화면에서 OwnYourPC를 실행할 수 있습니다.")
        p.setObjectName("muted")
        p.setWordWrap(True)
        l.addWidget(p)
        l.addStretch(1)
        return w

    # -- navigation -------------------------------------------------------
    def _sync(self, idx: int):
        self.stack.setCurrentIndex(idx)
        for i, lb in enumerate(self.step_labels):
            done = i < idx
            mark = "●" if i == idx else ("✓" if done else "○")
            lb.setText(f"{mark}  {self.STEPS[i]}")
            lb.setObjectName("stepActive" if i == idx else "step")
            lb.style().unpolish(lb)
            lb.style().polish(lb)
        self.btn_back.setVisible(idx in (1,))
        if idx == 0:
            self.btn_next.setText("다음")
        elif idx == 1:
            self.btn_next.setText("설치")
        elif idx == 2:
            self.btn_next.setEnabled(False)
            self.btn_back.setVisible(False)
            self.btn_cancel.setVisible(False)
        elif idx == 3:
            self.btn_next.setText("완료")
            self.btn_next.setEnabled(True)
            self.btn_cancel.setVisible(False)

    def _next(self):
        idx = self.stack.currentIndex()
        if idx == 0:
            self._sync(1)
        elif idx == 1:
            self._start_install()
        elif idx == 3:
            self._finish()

    def _back(self):
        if self.stack.currentIndex() == 1:
            self._sync(0)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "설치 위치 선택", self.path_edit.text())
        if d:
            self.path_edit.setText(str(Path(d) / APP_NAME))

    # -- install ----------------------------------------------------------
    def _start_install(self):
        dest = Path(self.path_edit.text().strip() or default_install_dir())
        self._sync(2)
        self.thread = QThread()
        self.worker = Installer(self.payload, dest, self.cb_desktop.isChecked())
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._on_progress)
        self.worker.log.connect(self.log_label.setText)
        self.worker.finished.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self._dest = dest
        self.thread.start()

    def _on_progress(self, done: int, total: int):
        self.bar.setValue(int(done * 100 / max(total, 1)))

    def _on_finished(self):
        self.thread.quit()
        self.bar.setValue(100)
        self._sync(3)

    def _on_failed(self, tb: str):
        self.thread.quit()
        QMessageBox.critical(self, "설치 실패", tb)
        self._sync(1)
        self.btn_next.setEnabled(True)
        self.btn_cancel.setVisible(True)

    def _finish(self):
        if getattr(self, "cb_launch", None) and self.cb_launch.isChecked():
            pyw = self._dest / "runtime" / "pythonw.exe"
            launcher = self._dest / "launcher" / "launcher.py"
            try:
                if pyw.exists():
                    subprocess.Popen([str(pyw), str(launcher)], cwd=str(self._dest))
            except Exception:  # noqa: BLE001
                pass
        self.close()

    # -- misc -------------------------------------------------------------
    def _peek_version(self):
        vf = self.payload / "version.txt"
        if vf.exists():
            try:
                return vf.read_text(encoding="utf-8-sig").strip() or "0.0.0"
            except Exception:
                pass
        return "0.0.0"

    def _logo(self):
        for base in (self.payload / "icons", self.payload / "src-tauri" / "icons"):
            for name in ("icon.png", "128x128.png"):
                p = base / name
                if p.exists():
                    return QPixmap(str(p)).scaled(56, 56, Qt.KeepAspectRatio,
                                                  Qt.SmoothTransformation)
        return None

    def _center(self):
        scr = QApplication.primaryScreen().geometry()
        self.move(scr.center().x() - self.width() // 2,
                  scr.center().y() - self.height() // 2)

    # draggable frameless window
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--payload", required=True, help="extracted payload dir")
    args = ap.parse_args()
    payload = Path(args.payload).resolve()

    app = QApplication(sys.argv)
    if not payload.is_dir():
        QMessageBox.critical(None, APP_NAME, f"페이로드 폴더를 찾을 수 없습니다:\n{payload}")
        sys.exit(1)
    wiz = Wizard(payload)
    wiz.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
