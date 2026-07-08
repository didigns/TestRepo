"""AISummary — uninstaller (PySide6 confirm + removal).

Registered in Add/Remove Programs; run on the app's private runtime:
    <InstallDir>\\runtime\\pythonw.exe <InstallDir>\\installer\\uninstall.py

Because the interpreter lives inside the install dir, we can't delete that dir
while running — so we hand the final rmdir off to a detached cmd that waits a
moment, then exits.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

APP_NAME = "AISummary"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DETACHED = 0x00000008 if os.name == "nt" else 0


def install_dir() -> Path:
    # installer/uninstall.py -> installer -> <InstallDir>
    return Path(__file__).resolve().parent.parent


def remove_shortcuts():
    if os.name != "nt":
        return
    appdata = os.environ.get("APPDATA", "")
    start = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / APP_NAME
    desktop = Path(os.path.expanduser("~")) / "Desktop" / f"{APP_NAME}.lnk"
    try:
        if start.exists():
            for f in start.glob("*"):
                f.unlink(missing_ok=True)
            start.rmdir()
    except Exception:  # noqa: BLE001
        pass
    try:
        desktop.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def remove_registry():
    if os.name != "nt":
        return
    import winreg
    try:
        winreg.DeleteKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Uninstall\\" + APP_NAME,
        )
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        pass


def stop_processes():
    if os.name != "nt":
        return
    for name in ("AISummary.exe",):
        subprocess.run(["taskkill", "/F", "/IM", name],
                       creationflags=CREATE_NO_WINDOW,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def schedule_delete(target: Path):
    """Detached cmd: wait, then remove the install dir (which holds us)."""
    if os.name != "nt":
        return
    cmd = f'ping 127.0.0.1 -n 2 >nul & rmdir /s /q "{target}"'
    subprocess.Popen(["cmd", "/c", cmd],
                     creationflags=CREATE_NO_WINDOW | DETACHED,
                     close_fds=True)


def main():
    app = QApplication(sys.argv)
    dest = install_dir()
    ans = QMessageBox.question(
        None, f"{APP_NAME} 제거",
        f"{APP_NAME}를 제거할까요?\n\n설치 폴더:\n{dest}\n\n"
        "(사용자 데이터 ~/.aisummary 는 유지됩니다.)",
        QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
    )
    if ans != QMessageBox.Yes:
        sys.exit(0)

    stop_processes()
    remove_shortcuts()
    remove_registry()
    schedule_delete(dest)
    QMessageBox.information(None, APP_NAME, "제거되었습니다.")
    sys.exit(0)


if __name__ == "__main__":
    main()
