"""Interactive Novel Maker — project store + flow-DSL assembly + plugin export.

A "novel project" is authored data whose *native format is the flow-DSL* the
plugin runtime already interprets (steps: text/generate/choice/input/if/end).
So store == runtime == export: no lossy compilation, fully round-trippable.

Projects live as JSON under ~/.aisummary/novels/<id>.json.
Exporting a project writes a self-contained plugin folder under the plugins
dir, making the finished novel a playable (and shareable) plugin — still with
network:false, so the zero-exposure guarantee holds by construction.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

from .config import NOVELS_DIR, PLUGINS_DIR

STEP_TYPES = ("text", "generate", "choice", "input", "if", "end")


# ---- id helpers ------------------------------------------------------
def _slug(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9가-힣]+", "-", (s or "").strip()).strip("-").lower()
    return s or "novel"


def _new_id() -> str:
    return "nv_" + hex(int(time.time() * 1000))[2:]


# ---- persistence -----------------------------------------------------
def _path(pid: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.\-]", "", pid or "")
    return NOVELS_DIR / f"{safe}.json"


def list_novels() -> list:
    NOVELS_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for f in sorted(NOVELS_DIR.glob("*.json")):
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({
            "id": p.get("id", f.stem),
            "title": p.get("title", "(제목 없음)"),
            "author": p.get("author", ""),
            "description": p.get("description", ""),
            "scenes": len(p.get("scenes", {}) or {}),
            "stats": len(p.get("stats", []) or []),
            "updated": p.get("updated", 0),
        })
    out.sort(key=lambda x: x.get("updated", 0), reverse=True)
    return out


def get_novel(pid: str) -> Optional[dict]:
    p = _path(pid)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_novel(project: dict) -> dict:
    NOVELS_DIR.mkdir(parents=True, exist_ok=True)
    proj = _normalize_project(project)
    _path(proj["id"]).write_text(
        json.dumps(proj, indent=2, ensure_ascii=False), encoding="utf-8")
    return proj


def delete_novel(pid: str) -> bool:
    p = _path(pid)
    if p.exists():
        p.unlink()
        return True
    return False


# ---- normalization / validation --------------------------------------
def _normalize_project(project: dict) -> dict:
    project = dict(project or {})
    now = int(time.time())
    project.setdefault("id", _new_id())
    project.setdefault("created", now)
    project["updated"] = now
    project["title"] = (project.get("title") or "제목 없는 소설").strip()
    project["author"] = (project.get("author") or "").strip()
    project["description"] = (project.get("description") or "").strip()
    project.setdefault("persona", "")
    # stats: list of {key,label,initial,max,bar,show}
    stats = []
    for s in (project.get("stats") or []):
        if not isinstance(s, dict) or not s.get("key"):
            continue
        stats.append({
            "key": re.sub(r"[^a-zA-Z0-9_]", "", str(s["key"]))[:24] or "v",
            "label": str(s.get("label", s["key"]))[:24],
            "initial": s.get("initial", 0),
            "max": s.get("max"),
            "bar": bool(s.get("bar", s.get("max") is not None)),
            "show": bool(s.get("show", True)),
        })
    project["stats"] = stats
    scenes = project.get("scenes")
    if not isinstance(scenes, dict):
        scenes = {}
    project["scenes"] = scenes
    order = project.get("order")
    if not isinstance(order, list):
        order = list(scenes.keys())
    project["order"] = [i for i in order if i in scenes] + \
        [i for i in scenes if i not in order]
    if not project.get("start") or project["start"] not in scenes:
        project["start"] = project["order"][0] if project["order"] else ""
    return project


# ---- plugin.json build + export --------------------------------------
def to_manifest(project: dict) -> dict:
    """Build a plugin manifest (contributes.mode) from a novel project."""
    proj = _normalize_project(project)
    status_rows = []
    for s in proj["stats"]:
        if not s.get("show"):
            continue
        row = {"label": s["label"], "value": "{{%s}}" % s["key"]}
        if s.get("bar") and s.get("max") is not None:
            row["bar"] = {"max": s["max"]}
        status_rows.append(row)
    mode = {
        "label": proj["title"],
        "icon": "📖",
        "persona": proj["persona"],
        "stats": proj["stats"],
        "flow": {"start": proj["start"], "steps": proj["scenes"]},
    }
    if status_rows:
        mode["status"] = {"title": proj["title"] + " · 상태", "rows": status_rows}
    return {
        "manifestVersion": 1,
        "id": "novel-" + _slug(proj["title"]),
        "name": proj["title"],
        "version": "1.0.0",
        "author": proj["author"] or "AISummary Novel Maker",
        "description": proj["description"] or "인터랙티브 소설 메이커로 제작된 소설.",
        "permissions": {
            "folders": [], "network": False,
            "capabilities": ["model:generate", "ui:render"],
        },
        "contributes": {"mode": mode},
    }


def export_plugin(pid: str) -> Optional[dict]:
    """Write the project as an installed plugin folder. Returns {id,path,name}."""
    proj = get_novel(pid)
    if not proj:
        return None
    manifest = to_manifest(proj)
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    folder = PLUGINS_DIR / manifest["id"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plugin.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"id": manifest["id"], "path": str(folder), "name": manifest["name"]}


# ---- AI draft: LLM output → normalized flow-DSL scenes ----------------
def draft_prompt(premise: str, tone: str, stats: list) -> str:
    stat_keys = ", ".join(s.get("key", "") for s in (stats or []) if s.get("key"))
    stat_hint = (
        f"\n사용할 스탯 변수(선택지 effects에서 증감): {stat_keys}"
        if stat_keys else "")
    return (
        "너는 인터랙티브 소설 설계자야. 아래 설정으로 분기형 소설의 '구조'를 설계해.\n"
        "반드시 아래 JSON 스키마 하나만 출력해. 설명·코드펜스 금지, 순수 JSON만.\n\n"
        "{\n"
        '  "scenes": [\n'
        '    {"id":"s1","narration":"장면 지문(2~4문장)",'
        '"choices":[{"label":"선택지","goto":"s2",'
        '"effects":[{"stat":"hp","delta":-10}]}]},\n'
        '    {"id":"s2","narration":"...","ending":true}\n'
        "  ]\n"
        "}\n\n"
        "규칙:\n"
        "- 장면 6~10개. 첫 장면 id는 s1.\n"
        "- 각 장면은 narration(지문) 필수. 분기 장면은 choices 2~3개.\n"
        "- choices[].goto 는 반드시 존재하는 다른 장면 id.\n"
        "- 결말 장면은 choices 없이 \"ending\":true.\n"
        "- effects 는 선택(없어도 됨), delta 는 정수." + stat_hint + "\n\n"
        f"[설정]\n분위기/장르: {tone or '자유'}\n전제: {premise}\n\n"
        "JSON:")


def _extract_json(raw: str) -> Optional[dict]:
    if not raw:
        return None
    txt = raw.strip()
    # strip code fences
    m = re.search(r"```(?:json)?\s*(.+?)```", txt, re.S)
    if m:
        txt = m.group(1).strip()
    # take the outermost braces
    a, b = txt.find("{"), txt.rfind("}")
    if a >= 0 and b > a:
        txt = txt[a:b + 1]
    for attempt in (txt, re.sub(r",\s*([}\]])", r"\1", txt)):
        try:
            obj = json.loads(attempt)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


def assemble_from_draft(raw: str, meta: dict) -> dict:
    """Turn the LLM's scene list into a normalized flow-DSL novel project.

    Each drafted scene → a `text` narration step; if it has choices → a
    following `choice` step; endings → an `end` step. Deterministic, so a
    weak local model only has to produce rough structure, not valid DSL.
    """
    parsed = _extract_json(raw) or {}
    dscenes = parsed.get("scenes")
    scenes: dict = {}
    order: list = []

    def add(sid, step):
        scenes[sid] = step
        order.append(sid)

    valid_ids = set()
    if isinstance(dscenes, list) and dscenes:
        for i, s in enumerate(dscenes):
            valid_ids.add(str(s.get("id") or f"s{i+1}"))
    if isinstance(dscenes, list) and dscenes:
        for i, s in enumerate(dscenes):
            if not isinstance(s, dict):
                continue
            sid = str(s.get("id") or f"s{i+1}")
            narr = str(s.get("narration") or s.get("text") or "").strip()
            choices = s.get("choices") if isinstance(s.get("choices"), list) else []
            is_end = bool(s.get("ending")) or not choices
            tid = sid + "_t"          # narration node
            cid = sid + "_c"          # choice node (if any)
            eid = sid                 # keep original id pointing at narration
            # narration step
            nxt = cid if choices else (tid + "_end")
            add(eid, {"type": "text", "text": narr or "…", "next": nxt})
            if choices:
                opts = []
                for c in choices:
                    if not isinstance(c, dict):
                        continue
                    goto = str(c.get("goto") or "")
                    if goto not in valid_ids:
                        goto = ""      # dangling → author fixes in editor
                    st = {}
                    for eff in (c.get("effects") or []):
                        if isinstance(eff, dict) and eff.get("stat"):
                            k = re.sub(r"[^a-zA-Z0-9_]", "", str(eff["stat"]))
                            try:
                                d = int(eff.get("delta", 0))
                            except Exception:
                                d = 0
                            if k and d:
                                sign = "+" if d >= 0 else "-"
                                st[k] = {"expr": f"{k}{sign}{abs(d)}"}
                    opt = {"label": str(c.get("label") or "선택"), "goto": goto}
                    if st:
                        opt["set"] = st
                    opts.append(opt)
                add(cid, {"type": "choice",
                          "prompt": str(s.get("prompt") or "어떻게 할까?"),
                          "options": opts})
            else:
                add(tid + "_end", {"type": "end"})
    if not scenes:
        # fallback skeleton so the user always gets something editable
        add("s1", {"type": "text",
                   "text": "(AI 초안 생성에 실패했습니다. 여기서 직접 편집하세요.)",
                   "next": "s1_end"})
        add("s1_end", {"type": "end"})

    return {
        "title": meta.get("title") or "새 인터랙티브 소설",
        "author": meta.get("author") or "",
        "description": meta.get("description") or (meta.get("premise") or "")[:120],
        "persona": meta.get("persona") or (
            "너는 몰입감 있는 인터랙티브 소설의 진행자야. 2인칭 시점, 생생하고 "
            "절제된 한국어 묘사. 장면은 짧게 써라."),
        "stats": meta.get("stats") or [],
        "start": order[0] if order else "",
        "scenes": scenes,
        "order": order,
    }
