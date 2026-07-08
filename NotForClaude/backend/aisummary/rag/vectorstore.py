"""LanceDB-backed vector store.

Stores chunk text + citation metadata + embedding in one table. Falls back
to a NumPy brute-force store if lancedb isn't installed (tests).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

from ..config import Settings, DB_DIR
from ..ingest.chunker import Chunk

TABLE = "chunks"


@dataclass
class Retrieved:
    chunk_id: str
    source_path: str
    filename: str
    page: int
    char_start: int
    char_end: int
    text: str
    score: float


class VectorStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.dim = settings.effective_embed_dim()
        self._db = None
        self._tbl = None
        self._fallback: Optional["_NumpyStore"] = None
        self._connect()

    def _connect(self) -> None:
        try:
            import lancedb
            import pyarrow as pa
            self._db = lancedb.connect(str(DB_DIR))
            if TABLE in self._db.table_names():
                self._tbl = self._db.open_table(TABLE)
            else:
                schema = pa.schema([
                    pa.field("chunk_id", pa.string()),
                    pa.field("source_path", pa.string()),
                    pa.field("filename", pa.string()),
                    pa.field("page", pa.int32()),
                    pa.field("char_start", pa.int32()),
                    pa.field("char_end", pa.int32()),
                    pa.field("text", pa.string()),
                    pa.field("vector", pa.list_(pa.float32(), self.dim)),
                ])
                self._tbl = self._db.create_table(TABLE, schema=schema)
        except ImportError:
            self._fallback = _NumpyStore()

    def add(self, chunks: List[Chunk], vectors: List[List[float]]) -> None:
        rows = [{
            "chunk_id": c.id, "source_path": c.source_path,
            "filename": c.metadata.get("filename", ""), "page": c.page,
            "char_start": c.char_start, "char_end": c.char_end,
            "text": c.text, "vector": v,
        } for c, v in zip(chunks, vectors)]
        if self._fallback is not None:
            self._fallback.add(rows)
        else:
            self._tbl.add(rows)

    def delete_source(self, source_path: str) -> None:
        if self._fallback is not None:
            self._fallback.delete_source(source_path)
        elif self._tbl is not None:
            safe = source_path.replace("'", "''")
            self._tbl.delete(f"source_path = '{safe}'")

    def search(self, query_vec: List[float], top_k: int) -> List[Retrieved]:
        if self._fallback is not None:
            hits = self._fallback.search(query_vec, top_k)
        else:
            res = self._tbl.search(query_vec).metric("cosine").limit(top_k).to_list()
            hits = []
            for r in res:
                sim = 1.0 - float(r.get("_distance", 0.0))
                hits.append((r, sim))
        return [Retrieved(
            chunk_id=r["chunk_id"], source_path=r["source_path"],
            filename=r.get("filename", ""), page=r["page"],
            char_start=r["char_start"], char_end=r["char_end"],
            text=r["text"], score=round(sim, 4),
        ) for r, sim in hits]

    def count(self) -> int:
        if self._fallback is not None:
            return len(self._fallback.rows)
        return self._tbl.count_rows() if self._tbl is not None else 0

    def all_rows(self) -> List[dict]:
        """All chunks (without vectors) — used to build the BM25 index."""
        if self._fallback is not None:
            return [{k: v for k, v in r.items() if k != "vector"}
                    for r in self._fallback.rows]
        if self._tbl is None:
            return []
        rows = self._tbl.to_arrow().to_pylist()
        for r in rows:
            r.pop("vector", None)
        return rows


class _NumpyStore:
    """Brute-force cosine store used when lancedb is unavailable (tests)."""
    def __init__(self):
        self.rows: list = []

    def add(self, rows):
        self.rows.extend(rows)

    def delete_source(self, source_path):
        self.rows = [r for r in self.rows if r["source_path"] != source_path]

    def search(self, q, top_k):
        def cos(a, b):
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a)) or 1e-9
            nb = math.sqrt(sum(y * y for y in b)) or 1e-9
            return dot / (na * nb)
        scored = [(r, cos(q, r["vector"])) for r in self.rows]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]
