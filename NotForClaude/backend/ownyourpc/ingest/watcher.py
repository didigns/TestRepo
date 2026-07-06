"""Folder watcher + ingestion orchestration.

Uses watchdog for live events. Change detection via SHA-256 + mtime so a
restart never re-ingests unchanged files. Deleted files are removed from
the vector store.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from ..config import Settings, DATA_DIR
from ..llm import OllamaProvider
from ..rag.vectorstore import VectorStore
from . import parsers
from .chunker import chunk_blocks

STATE_PATH = DATA_DIR / "ingest_state.json"


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


class Ingestor:
    def __init__(self, settings: Settings, store: VectorStore,
                 provider: OllamaProvider, on_event: Optional[Callable] = None):
        self.settings = settings
        self.store = store
        self.provider = provider
        self.on_event = on_event or (lambda *_: None)
        self.state = self._load_state()

    # ---- state -------------------------------------------------------
    def _load_state(self) -> dict:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return {}

    def _save_state(self) -> None:
        STATE_PATH.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    # ---- single file -------------------------------------------------
    def ingest_file(self, path: Path) -> int:
        if not parsers.is_supported(path) or not path.exists():
            return 0
        digest = _file_hash(path)
        if self.state.get(str(path), {}).get("hash") == digest:
            return 0  # unchanged

        self.on_event("parsing", str(path))
        blocks = parsers.parse(path)
        chunks = chunk_blocks(path, blocks,
                              chunk_tokens=self.settings.profile().chunk_tokens)
        if not chunks:
            return 0

        self.on_event("embedding", f"{path.name}: {len(chunks)} chunks")
        vectors = self.provider.embed_batch([c.text for c in chunks])

        self.store.delete_source(str(path))  # replace prior version
        self.store.add(chunks, vectors)

        self.state[str(path)] = {"hash": digest, "chunks": len(chunks),
                                 "ts": time.time()}
        self._save_state()
        self.on_event("indexed", f"{path.name}: {len(chunks)} chunks")
        return len(chunks)

    def remove_file(self, path: Path) -> None:
        self.store.delete_source(str(path))
        self.state.pop(str(path), None)
        self._save_state()
        self.on_event("removed", str(path))

    # ---- full scan ---------------------------------------------------
    def scan_folder(self, folder: Path) -> dict:
        total_files = total_chunks = 0
        for p in folder.rglob("*"):
            if p.is_file() and parsers.is_supported(p):
                n = self.ingest_file(p)
                if n:
                    total_files += 1
                    total_chunks += n
        return {"files": total_files, "chunks": total_chunks}

    # ---- live watching ----------------------------------------------
    def watch(self, folders: list[str]) -> "WatchHandle":
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        ingestor = self

        class Handler(FileSystemEventHandler):
            def on_created(self, e):
                if not e.is_directory:
                    ingestor.ingest_file(Path(e.src_path))

            def on_modified(self, e):
                if not e.is_directory:
                    ingestor.ingest_file(Path(e.src_path))

            def on_deleted(self, e):
                if not e.is_directory:
                    ingestor.remove_file(Path(e.src_path))

        observer = Observer()
        for folder in folders:
            observer.schedule(Handler(), folder, recursive=True)
        observer.start()
        return WatchHandle(observer)


class WatchHandle:
    def __init__(self, observer):
        self._observer = observer

    def stop(self):
        self._observer.stop()
        self._observer.join(timeout=5)
