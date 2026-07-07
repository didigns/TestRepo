"""OwnYourPC update logic — shared by the launcher and installer.

Pure standard library (urllib, hashlib, json) so it runs on a minimal
embedded Python with no third-party dependencies.

The app reads a small `.meta` JSON hosted on Google Drive:

    {
      "version": "0.2.0",
      "notes": "...",
      "mandatory": false,
      "installer": {
        "filename": "OwnYourPC-setup.exe",
        "url": "https://drive.google.com/file/d/<ID>/view?...",
        "size": 3549696,
        "sha256": "<64 hex>"
      }
    }
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

# Drive file id of the `.meta` document the app reads.
DEFAULT_META_ID = "1hsD3y7bAJYCDdE1TR31gE6odYVWEy2T_"

_UA = "OwnYourPC-Updater/1.0"


def drive_download_url(file_id: str) -> str:
    """Direct-download URL that bypasses Drive's large-file scan page."""
    return (
        "https://drive.usercontent.google.com/download"
        f"?id={file_id}&export=download&confirm=t"
    )


def extract_drive_id(url: str) -> str:
    """Pull a Drive file id from the common URL shapes, else return as-is."""
    if "/d/" in url:
        return url.split("/d/", 1)[1].split("/", 1)[0]
    if "id=" in url:
        tail = url.split("id=", 1)[1]
        out = []
        for ch in tail:
            if ch == "&":
                break
            out.append(ch)
        if out:
            return "".join(out)
    return url


def meta_url() -> str:
    env = os.environ.get("OYPC_META_URL", "").strip()
    if env:
        return env
    file_id = os.environ.get("OYPC_META_ID", "").strip() or DEFAULT_META_ID
    return drive_download_url(file_id)


@dataclass
class Installer:
    filename: str
    url: str
    size: int = 0
    sha256: str = ""


@dataclass
class Meta:
    version: str
    notes: str
    mandatory: bool
    installer: Installer

    @staticmethod
    def from_dict(d: dict) -> "Meta":
        inst = d.get("installer", {}) or {}
        return Meta(
            version=str(d.get("version", "0.0.0")).strip(),
            notes=str(d.get("notes", "")),
            mandatory=bool(d.get("mandatory", False)),
            installer=Installer(
                filename=str(inst.get("filename", "OwnYourPC-setup.exe")),
                url=str(inst.get("url", "")),
                size=int(inst.get("size", 0) or 0),
                sha256=str(inst.get("sha256", "")),
            ),
        )


def _open(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_meta(timeout: int = 20) -> Meta:
    """Fetch and parse the remote .meta. Raises on network/parse error."""
    with _open(meta_url(), timeout=timeout) as resp:
        raw = resp.read().decode("utf-8-sig")  # tolerate a BOM
    return Meta.from_dict(json.loads(raw))


def _parse_semver(s: str):
    core = s.strip().lstrip("v").split("-")[0].split("+")[0]
    parts = core.split(".")
    try:
        nums = tuple(int(p) for p in parts)
        return nums if len(nums) == 3 else None
    except ValueError:
        return None


def is_newer(remote: str, current: str) -> bool:
    """True when `remote` is strictly newer than `current`."""
    rv, cv = _parse_semver(remote), _parse_semver(current)
    if rv is not None and cv is not None:
        # prerelease (has '-') is considered lower than the release
        if rv == cv:
            r_pre = "-" in remote
            c_pre = "-" in current
            return c_pre and not r_pre
        return rv > cv
    # loose fallback: compare integer runs
    def runs(x):
        out, cur = [], ""
        for ch in x:
            if ch.isdigit():
                cur += ch
            elif cur:
                out.append(int(cur))
                cur = ""
        if cur:
            out.append(int(cur))
        return out

    ra, rb = runs(remote), runs(current)
    for i in range(max(len(ra), len(rb))):
        a = ra[i] if i < len(ra) else 0
        b = rb[i] if i < len(rb) else 0
        if a != b:
            return a > b
    return False


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_installer(
    installer: Installer,
    dest_dir: Optional[str] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> str:
    """Download the installer, verify SHA-256 (when provided), return path.

    `progress(downloaded, total)` is called periodically; `total` is 0 if the
    server does not report Content-Length.
    """
    file_id = extract_drive_id(installer.url)
    url = drive_download_url(file_id)
    dest_dir = dest_dir or tempfile.gettempdir()
    os.makedirs(dest_dir, exist_ok=True)
    name = _sanitize(installer.filename)
    out_path = os.path.join(dest_dir, name)

    with _open(url, timeout=60) as resp:
        ctype = resp.headers.get("Content-Type", "")
        total = int(resp.headers.get("Content-Length", installer.size or 0) or 0)
        downloaded = 0
        first = b""
        with open(out_path, "wb") as f:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                if downloaded == 0:
                    first = chunk[:64]
                f.write(chunk)
                downloaded += len(chunk)
                if progress:
                    progress(downloaded, total)

    # An HTML payload means Drive returned an interstitial, not the file.
    if "text/html" in ctype and downloaded < 100_000:
        raise RuntimeError(
            "Drive가 파일 대신 HTML을 반환했습니다. 파일 공유 설정('링크가 있는 "
            "모든 사용자')과 파일 ID를 확인하세요."
        )

    want = (installer.sha256 or "").strip().lower()
    if want and set(want) != {"0"}:
        got = sha256_file(out_path)
        if got.lower() != want:
            raise RuntimeError(f"SHA-256 불일치 — 예상 {want}, 실제 {got}")

    return out_path


def _sanitize(name: str) -> str:
    name = (name or "").strip()
    bad = set('/\\:*?"<>|')
    cleaned = "".join(c for c in name if c not in bad)
    return cleaned or "OwnYourPC-setup.exe"
