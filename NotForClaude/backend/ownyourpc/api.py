"""FastAPI local service. Binds to 127.0.0.1 only. Token-authenticated.

Endpoints:
  GET  /health              service + ollama status
  GET  /hardware            detect hardware + recommend tier
  POST /settings            update settings (tier, folders, params)
  POST /ingest/scan         full scan of a folder
  POST /query               grounded RAG query
  POST /meetings/summarize  summarize a saved transcript -> minutes + PDF
"""
from __future__ import annotations

import secrets
from pathlib import Path
from typing import Optional

from fastapi import (FastAPI, Depends, Header, HTTPException, Query,
                     WebSocket, WebSocketDisconnect)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .config import load_settings, save_settings, Settings, TIERS
from .hardware import recommend
from .providers import make_provider
from .rag.vectorstore import VectorStore
from .rag.engine import RagEngine
from .ingest.watcher import Ingestor

# Session token: generated per process; the desktop shell reads it and
# attaches it to every request. Prevents other local processes calling us.
def _load_token() -> str:
    """Persist the session token so auto-restarts keep existing pages working."""
    from .config import DATA_DIR, ensure_dirs
    ensure_dirs()
    tp = DATA_DIR / ".session_token"
    if tp.exists():
        tok = tp.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(24)
    try:
        tp.write_text(tok, encoding="utf-8")
    except Exception:
        pass
    return tok


SESSION_TOKEN = _load_token()

app = FastAPI(title="OwnYourPC", version=__version__)
app.add_middleware(
    CORSMiddleware, allow_origins=["http://127.0.0.1", "http://localhost"],
    allow_methods=["*"], allow_headers=["*"],
)

_state: dict = {}


def _boot():
    settings = load_settings()
    provider = make_provider(settings)
    store = VectorStore(settings)
    _state.update({
        "settings": settings, "provider": provider, "store": store,
        "engine": RagEngine(settings, store, provider),
        "ingestor": Ingestor(settings, store, provider),
    })


@app.on_event("startup")
def startup():
    _boot()


def auth(x_token: str = Header(default="")):
    if not secrets.compare_digest(x_token, SESSION_TOKEN):
        raise HTTPException(status_code=401, detail="invalid token")


# ---- schemas ---------------------------------------------------------
class QueryReq(BaseModel):
    question: str


class ScanReq(BaseModel):
    folder: str


class SettingsReq(BaseModel):
    tier: Optional[str] = None
    watch_folders: Optional[list[str]] = None
    temperature: Optional[float] = None
    similarity_threshold: Optional[float] = None
    hybrid_search: Optional[bool] = None
    top_k: Optional[int] = None           # retrieved docs per query
    require_citations: Optional[bool] = None
    backend: Optional[str] = None         # "llamacpp" | "ollama"
    stt_language: Optional[str] = None    # "ko" | "en" | "auto" (=> None)
    stt_model: Optional[str] = None       # tiny | base | small | medium
    stt_device: Optional[str] = None      # "cpu" | "cuda"
    diarize_enabled: Optional[bool] = None
    hf_token: Optional[str] = None


class SummarizeReq(BaseModel):
    transcript_path: str


# ---- endpoints -------------------------------------------------------
FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "index.html"
_ICONS = FRONTEND.parent / "icons"
if _ICONS.is_dir():
    app.mount("/icons", StaticFiles(directory=str(_ICONS)), name="icons")
_VENDOR = FRONTEND.parent / "vendor"
if _VENDOR.is_dir():
    app.mount("/vendor", StaticFiles(directory=str(_VENDOR)), name="vendor")


@app.get("/", response_class=HTMLResponse)
def ui():
    """Serve the SPA with the session token injected (same-origin API calls)."""
    if not FRONTEND.exists():
        return HTMLResponse("<h1>OwnYourPC</h1><p>frontend/index.html 없음</p>")
    html = FRONTEND.read_text(encoding="utf-8")
    # never cache the shell so UI updates always take effect on reload
    return HTMLResponse(html.replace("__OWNYOURPC_TOKEN__", SESSION_TOKEN),
                        headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/health")
def health():
    prov = _state["provider"]
    return {
        "service": "ok", "version": __version__,
        "ollama_up": prov.is_up(),
        "installed_models": prov.installed_models(),
        "indexed_chunks": _state["store"].count(),
    }


@app.get("/hardware")
def hardware(_=Depends(auth)):
    return recommend()


@app.post("/settings")
def update_settings(req: SettingsReq, _=Depends(auth)):
    s: Settings = _state["settings"]
    if req.tier and req.tier in TIERS:
        s.tier = req.tier
    if req.watch_folders is not None:
        s.watch_folders = req.watch_folders
    if req.temperature is not None:
        s.temperature = req.temperature
    if req.similarity_threshold is not None:
        s.similarity_threshold = req.similarity_threshold
    if req.hybrid_search is not None:
        s.hybrid_search = req.hybrid_search
    if req.stt_language is not None:
        s.stt_language = None if req.stt_language == "auto" else req.stt_language
    if req.stt_model is not None:
        s.stt_model_override = None if req.stt_model in ("", "auto") else req.stt_model
    if req.top_k is not None:
        s.top_k_override = int(req.top_k) or None
    if req.require_citations is not None:
        s.require_citations = req.require_citations
    if req.backend in ("llamacpp", "ollama"):
        s.backend = req.backend
    if req.stt_device in ("cpu", "cuda"):
        s.stt_device = req.stt_device
    if req.diarize_enabled is not None:
        s.diarize_enabled = req.diarize_enabled
    if req.hf_token is not None:
        s.hf_token = req.hf_token.strip()
    save_settings(s)
    _boot()  # rebuild with new tier/models
    return {"ok": True, "settings": s.__dict__}


@app.get("/settings")
def get_settings(_=Depends(auth)):
    s: Settings = _state["settings"]
    prof = s.profile()
    return {
        **{k: v for k, v in s.__dict__.items()},
        "effective_top_k": s.top_k_override or prof.top_k,
        "profile": {"llm_model": prof.llm_model, "embed_model": prof.embed_model,
                    "stt_model": prof.stt_model, "top_k": prof.top_k,
                    "context_tokens": prof.context_tokens},
    }


_PULL_STATUS: dict = {}


def _set_pull(key, **kw):
    """Merge progress fields into the per-key status dict."""
    _PULL_STATUS.setdefault(key, {}).update(kw)


@app.get("/models")
def models_status(_=Depends(auth)):
    """Which LLM / embedding / STT models are downloaded on this machine."""
    from pathlib import Path as _P
    from .config import LLAMACPP_LLM, LLAMACPP_EMBED, MODELS_CACHE
    s: Settings = _state["settings"]
    items = []
    if s.backend == "llamacpp":
        present = ({p.name for p in MODELS_CACHE.glob("*.gguf")}
                   if MODELS_CACHE.exists() else set())
        for tier, (repo, fn) in LLAMACPP_LLM.items():
            items.append({"group": "LLM", "name": f"Gemma 4 · {tier}",
                          "detail": fn, "installed": fn in present, "key": f"llm:{tier}"})
        erepo, efn, _d = LLAMACPP_EMBED
        items.append({"group": "임베딩", "name": "nomic-embed-text", "detail": efn,
                      "installed": efn in present, "key": "embed"})
    else:
        inst = _state["provider"].installed_models()
        prof = s.profile()
        for nm in (prof.llm_model, prof.embed_model):
            items.append({"group": "LLM/임베딩", "name": nm, "detail": "ollama",
                          "installed": any(nm.split(":")[0] in i for i in inst),
                          "key": f"ollama:{nm}"})
    hub = _P.home() / ".cache" / "huggingface" / "hub"
    for size in ("tiny", "base", "small", "medium"):
        cached = (hub / f"models--Systran--faster-whisper-{size}").exists()
        items.append({"group": "STT · Whisper", "name": size, "detail": "faster-whisper",
                      "installed": cached, "key": f"stt:{size}"})
    try:
        from .meetings import diarize as _diar
        items.append({"group": "화자 분리 · ONNX", "name": "화자 분리 모델",
                      "detail": "sherpa-onnx", "installed": _diar.models_installed(),
                      "key": "diar"})
    except Exception:
        pass
    return {"backend": s.backend, "items": items}


class PullReq(BaseModel):
    key: str


def _hf_url(repo: str, fn: str) -> str:
    from huggingface_hub import hf_hub_url
    return hf_hub_url(repo_id=repo, filename=fn)


def _download_gguf(repo: str, fn: str, key: str):
    """Stream a GGUF into MODELS_CACHE reporting byte-level progress."""
    import requests
    from .config import MODELS_CACHE
    MODELS_CACHE.mkdir(parents=True, exist_ok=True)
    dest = MODELS_CACHE / fn
    if dest.exists():
        _set_pull(key, state="done", pct=100)
        return
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(_hf_url(repo, fn), stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0) or 0)
        done = 0
        _set_pull(key, state="downloading", pct=0, downloaded=0, total=total)
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                pct = int(done * 100 / total) if total else -1
                _set_pull(key, state="downloading", pct=pct, downloaded=done, total=total)
    tmp.replace(dest)
    _set_pull(key, state="done", pct=100, downloaded=done, total=total)


@app.post("/models/pull")
def models_pull(req: PullReq, _=Depends(auth)):
    """Download a model in the background; poll /models/pull-status.

    Status per key: {"state": downloading|done|error, "pct": 0-100 or -1
    (indeterminate), "downloaded", "total", "error"}.
    """
    import threading
    from .config import LLAMACPP_LLM, LLAMACPP_EMBED
    key = req.key

    def run():
        _set_pull(key, state="downloading", pct=-1, error="")
        try:
            if key.startswith("llm:"):
                repo, fn = LLAMACPP_LLM[key.split(":", 1)[1]]
                _download_gguf(repo, fn, key)
            elif key == "embed":
                repo, fn, _d = LLAMACPP_EMBED
                _download_gguf(repo, fn, key)
            elif key.startswith("stt:"):
                # faster-whisper pulls a multi-file repo; show indeterminate.
                from faster_whisper import WhisperModel
                WhisperModel(key.split(":", 1)[1], device="cpu", compute_type="int8")
                _set_pull(key, state="done", pct=100)
            elif key.startswith("ollama:"):
                import subprocess
                subprocess.run(["ollama", "pull", key.split(":", 1)[1]], check=False)
                _set_pull(key, state="done", pct=100)
            elif key == "diar":
                from .meetings import diarize as _diar
                _diar.ensure_models()
                _set_pull(key, state="done", pct=100)
            else:
                _set_pull(key, state="done", pct=100)
        except Exception as e:
            _set_pull(key, state="error", pct=-1, error=str(e)[:160])

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


@app.get("/models/pull-status")
def models_pull_status(_=Depends(auth)):
    return _PULL_STATUS


@app.post("/ingest/scan")
def scan(req: ScanReq, _=Depends(auth)):
    folder = Path(req.folder)
    if not folder.is_dir():
        raise HTTPException(400, "folder not found")
    res = _state["ingestor"].scan_folder(folder)
    s: Settings = _state["settings"]
    if str(folder) not in s.watch_folders:
        s.watch_folders.append(str(folder))
        save_settings(s)
    return res


@app.post("/pick-folder")
def pick_folder(_=Depends(auth)):
    """Open the OS-native folder picker on the user's desktop (tkinter runs
    in a subprocess so it never blocks or conflicts with the server loop).
    Returns {path: ""} if the user cancels."""
    import subprocess
    import sys
    script = (
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
        "print(filedialog.askdirectory(title='감시할 폴더 선택') or '')\n"
    )
    try:
        out = subprocess.run([sys.executable, "-c", script],
                             capture_output=True, text=True, timeout=300)
        return {"path": out.stdout.strip()}
    except Exception as e:
        raise HTTPException(500, f"folder picker unavailable: {e}")


@app.post("/query")
def query(req: QueryReq, _=Depends(auth)):
    ans = _state["engine"].query(req.question)
    return {
        "answer": ans.text, "grounded": ans.grounded, "refused": ans.refused,
        "confidence": ans.confidence, "warnings": ans.warnings,
        "citations": [c.__dict__ for c in ans.citations],
    }


@app.post("/meetings/summarize")
def summarize_meeting(req: SummarizeReq, _=Depends(auth)):
    import json
    from .meetings.transcribe import Transcript, Segment
    from .meetings.minutes import summarize, render_pdf

    data = json.loads(Path(req.transcript_path).read_text(encoding="utf-8"))
    tr = Transcript(
        meeting_id=data["meeting_id"], title=data["title"],
        started_at=data["started_at"],
        segments=[Segment(**s) for s in data["segments"]],
    )
    minutes = summarize(tr, _state["settings"], _state["provider"])
    pdf = render_pdf(minutes, tr)
    return {"minutes": minutes.__dict__, "pdf_path": str(pdf)}


def auth_q(token: str = Query(default="")):
    """Auth for GET endpoints that can't set headers (img src, EventSource)."""
    if not secrets.compare_digest(token, SESSION_TOKEN):
        raise HTTPException(status_code=401, detail="invalid token")


@app.post("/query/stream")
def query_stream(req: QueryReq, _=Depends(auth)):
    import json as _json

    def gen():
        for ev in _state["engine"].query_stream(req.question):
            yield f"data: {_json.dumps(ev, ensure_ascii=False)}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/kb")
def knowledge_base(_=Depends(auth)):
    from .rag.preview import kb_summary
    return kb_summary(_state["settings"])


@app.get("/kb/files")
def kb_files(folder: str, _=Depends(auth)):
    from .rag.preview import list_files
    return {"files": list_files(_state["settings"], folder)}


@app.get("/source/page.png")
def source_page(path: str, page: int = 1, _=Depends(auth_q)):
    from .rag.preview import is_indexed, render_page_png
    if not is_indexed(path):
        raise HTTPException(403, "not an indexed file")
    png = render_page_png(path, page)
    if png is None:
        raise HTTPException(404, "not a pdf")
    return Response(content=png, media_type="image/png")


@app.get("/source/highlights")
def source_highlights(path: str, page: int = 1, q: str = "", _=Depends(auth_q)):
    from .rag.preview import is_indexed, find_highlights, page_count
    if not is_indexed(path):
        raise HTTPException(403, "not an indexed file")
    return {"rects": find_highlights(path, page, q), "pages": page_count(path)}


@app.get("/source/text")
def source_text(path: str, q: str = "", raw: int = 0, _=Depends(auth_q)):
    from .rag.preview import is_indexed, text_preview, file_text
    if not is_indexed(path):
        raise HTTPException(403, "not an indexed file")
    if raw:                       # full text (markdown render highlights in DOM)
        return {"text": file_text(path)[:60000], "ranges": []}
    return text_preview(path, q)


class StartMeetingReq(BaseModel):
    title: str = "회의"
    source: str = "mic"        # "mic" | "system" (loopback)


class OpenFileReq(BaseModel):
    path: str


def _path_allowed(path: str) -> bool:
    """Allow opening files under the app data dir, indexed files, or files
    inside a watched folder — nothing else."""
    import os
    from .config import DATA_DIR
    from .rag.preview import is_indexed
    p = os.path.normpath(path)
    if p.startswith(os.path.normpath(str(DATA_DIR))):
        return True
    if is_indexed(path):
        return True
    return any(p.startswith(os.path.normpath(f))
               for f in _state["settings"].watch_folders)


@app.post("/open-file")
def open_file(req: OpenFileReq, _=Depends(auth)):
    """Open a file with the OS default app (meeting PDF/MD, indexed docs)."""
    import os
    import subprocess
    import sys
    p = os.path.normpath(req.path)
    if not _path_allowed(req.path):
        raise HTTPException(403, "not allowed")
    if not os.path.exists(p):
        raise HTTPException(404, "file not found")
    try:
        if sys.platform.startswith("win"):
            os.startfile(p)                      # noqa: type-ignore (win only)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/reveal")
def reveal_file(req: OpenFileReq, _=Depends(auth)):
    """Reveal a file in the OS file manager (Explorer/Finder)."""
    import os
    import subprocess
    import sys
    p = os.path.normpath(req.path)
    if not _path_allowed(req.path) or not os.path.exists(p):
        raise HTTPException(404, "not allowed / not found")
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(f'explorer /select,"{p}"')
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", p])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(p)])
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


class PathReq(BaseModel):
    path: str


@app.post("/ingest/file")
def ingest_one(req: PathReq, _=Depends(auth)):
    from pathlib import Path as _P
    n = _state["ingestor"].ingest_file(_P(req.path))
    return {"chunks": n}


@app.post("/ingest/remove")
def ingest_remove(req: PathReq, _=Depends(auth)):
    from pathlib import Path as _P
    _state["ingestor"].remove_file(_P(req.path))
    return {"ok": True}


@app.post("/meetings/start")
def meeting_start(req: StartMeetingReq, _=Depends(auth)):
    from .meetings.session import MANAGER
    if req.source == "system":
        try:
            import soundcard  # noqa: F401
        except ImportError:
            raise HTTPException(
                400, "시스템 소리 캡처에는 soundcard 설치가 필요합니다: pip install soundcard")
    sid = MANAGER.start(_state["settings"], req.title, source=req.source)
    return {"meeting_id": sid}


@app.get("/meetings/list")
def meetings_list(_=Depends(auth)):
    from .meetings.store import list_grouped
    return {"groups": list_grouped()}


class TitleReq(BaseModel):
    text: str


@app.post("/meetings/suggest-title")
def meeting_suggest_title(req: TitleReq, _=Depends(auth)):
    from .meetings.minutes import suggest_title
    return {"title": suggest_title(req.text, _state["provider"])}


@app.post("/meetings/live-summary")
def meeting_live_summary(req: TitleReq, _=Depends(auth)):
    from .meetings.minutes import live_summary
    return {"markdown": live_summary(req.text, _state["provider"])}


@app.websocket("/meetings/{sid}/stream")
async def meeting_stream(ws: WebSocket, sid: str):
    import asyncio
    from .meetings.session import MANAGER
    await ws.accept()
    try:
        while True:
            for seg in MANAGER.poll(sid):
                await ws.send_json(seg)
            if MANAGER.is_done(sid):
                await ws.send_json({"event": "done"})
                break
            await asyncio.sleep(0.3)
    except WebSocketDisconnect:
        pass


@app.post("/meetings/{sid}/stop")
def meeting_stop(sid: str, _=Depends(auth)):
    from .meetings.session import MANAGER
    try:
        return MANAGER.stop(sid, _state["settings"], _state["provider"])
    except KeyError:
        raise HTTPException(404, "meeting not found")


@app.get("/meetings/{sid}")
def meeting_get(sid: str, _=Depends(auth)):
    from .meetings.store import get_record
    rec = get_record(sid)
    if not rec:
        raise HTTPException(404, "meeting not found")
    return rec


class RenameReq(BaseModel):
    title: str


@app.post("/meetings/{sid}/rename")
def meeting_rename(sid: str, req: RenameReq, _=Depends(auth)):
    import json
    from .config import MEETINGS_DIR
    from .meetings.store import get_record
    rec = get_record(sid)
    if not rec:
        raise HTTPException(404, "meeting not found")
    rec["title"] = req.title.strip()[:60] or rec.get("title", "회의")
    (MEETINGS_DIR / f"{sid}.json").write_text(
        json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "title": rec["title"]}


@app.post("/meetings/{sid}/delete")
def meeting_delete(sid: str, _=Depends(auth)):
    from .config import MEETINGS_DIR
    for ext in ("json", "md", "pdf", "wav"):
        try:
            (MEETINGS_DIR / f"{sid}.{ext}").unlink(missing_ok=True)
        except Exception:
            pass
    return {"ok": True}


def print_token():
    print(f"OWNYOURPC_TOKEN={SESSION_TOKEN}")


if __name__ == "__main__":
    import uvicorn
    print_token()
    uvicorn.run(app, host="127.0.0.1", port=8756)
