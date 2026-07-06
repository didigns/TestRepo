"""Meeting persistence + date-grouped listing.

Each meeting is one JSON record under MEETINGS_DIR holding the transcript,
minutes, and paths to the generated .md / .pdf. Listing groups meetings by
month -> ISO-ish week (newest first) for the left-rail list.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Optional

from ..config import MEETINGS_DIR


def _records():
    if not MEETINGS_DIR.exists():
        return
    for p in MEETINGS_DIR.glob("m*.json"):
        try:
            yield json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue


def save_record(meeting_id: str, title: str, started_at: float,
                transcript: dict, minutes: dict, md_path, pdf_path) -> dict:
    MEETINGS_DIR.mkdir(parents=True, exist_ok=True)
    rec = {
        "meeting_id": meeting_id, "title": title, "started_at": started_at,
        "transcript": transcript, "minutes": minutes,
        "md_path": str(md_path), "pdf_path": str(pdf_path),
    }
    (MEETINGS_DIR / f"{meeting_id}.json").write_text(
        json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
    return rec


def get_record(meeting_id: str) -> Optional[dict]:
    p = MEETINGS_DIR / f"{meeting_id}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def list_grouped() -> list:
    """[{month, weeks:[{week, meetings:[{meeting_id,title,day,date}]}]}] newest first."""
    recs = sorted(_records(), key=lambda d: d.get("started_at") or 0, reverse=True)
    months: list = []
    midx: dict = {}
    for d in recs:
        ts = d.get("started_at") or 0
        dt = datetime.datetime.fromtimestamp(ts)
        mkey = (dt.year, dt.month)
        wnum = (dt.day - 1) // 7 + 1
        m = midx.get(mkey)
        if m is None:
            m = {"month": f"{dt.year}년 {dt.month}월", "weeks": [], "_w": {}}
            midx[mkey] = m
            months.append(m)
        w = m["_w"].get(wnum)
        if w is None:
            w = {"week": f"{wnum}주차", "meetings": []}
            m["_w"][wnum] = w
            m["weeks"].append(w)
        w["meetings"].append({
            "meeting_id": d.get("meeting_id"),
            "title": d.get("title", "회의"),
            "day": dt.strftime("%m/%d"),
            "date": dt.strftime("%Y-%m-%d %H:%M"),
        })
    for m in months:
        m.pop("_w", None)
    return months
