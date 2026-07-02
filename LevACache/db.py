"""SQLite 캐시 저장소. 파일별 요약/키워드를 증분(해시) 기반으로 캐싱."""
import json
import os
import sqlite3
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS file_cache (
    path         TEXT PRIMARY KEY,
    filename     TEXT NOT NULL,
    ext          TEXT,
    size_bytes   INTEGER,
    mtime        TEXT,
    content_hash TEXT,
    model_key    TEXT,        -- 'vision' | 'text'
    model_name   TEXT,        -- 실제 gguf 파일명
    keywords     TEXT,        -- JSON 배열 문자열
    summary      TEXT,
    status       TEXT,        -- PENDING | CACHED | FAILED
    error_msg    TEXT,
    cached_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_cache_hash ON file_cache(content_hash);
CREATE INDEX IF NOT EXISTS idx_cache_status ON file_cache(status);
"""


def connect(db_path):
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def get_entry(conn, path):
    row = conn.execute("SELECT * FROM file_cache WHERE path = ?", (path,)).fetchone()
    return dict(row) if row else None


def needs_caching(conn, path, content_hash):
    """캐시가 없거나 해시가 다르거나 CACHED가 아니면 True."""
    row = get_entry(conn, path)
    if row is None:
        return True
    if row.get("content_hash") != content_hash:
        return True
    return row.get("status") != "CACHED"


def upsert_pending(conn, *, path, filename, ext, size_bytes, mtime, content_hash, model_key, model_name):
    conn.execute(
        """INSERT INTO file_cache(path, filename, ext, size_bytes, mtime, content_hash,
                                  model_key, model_name, status, cached_at)
           VALUES(?,?,?,?,?,?,?,?, 'PENDING', ?)
           ON CONFLICT(path) DO UPDATE SET
               filename=excluded.filename, ext=excluded.ext, size_bytes=excluded.size_bytes,
               mtime=excluded.mtime, content_hash=excluded.content_hash,
               model_key=excluded.model_key, model_name=excluded.model_name,
               status='PENDING', error_msg=NULL""",
        (path, filename, ext, size_bytes, mtime, content_hash, model_key, model_name,
         datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def set_cached(conn, path, keywords, summary):
    conn.execute(
        """UPDATE file_cache SET keywords=?, summary=?, status='CACHED', error_msg=NULL,
               cached_at=? WHERE path=?""",
        (json.dumps(keywords, ensure_ascii=False), summary,
         datetime.now().isoformat(timespec="seconds"), path),
    )
    conn.commit()


def set_failed(conn, path, error_msg):
    conn.execute(
        "UPDATE file_cache SET status='FAILED', error_msg=?, cached_at=? WHERE path=?",
        (str(error_msg)[:500], datetime.now().isoformat(timespec="seconds"), path),
    )
    conn.commit()


def remove(conn, path):
    conn.execute("DELETE FROM file_cache WHERE path=?", (path,))
    conn.commit()


def list_entries(conn, limit=200):
    rows = conn.execute(
        "SELECT path, filename, ext, model_key, keywords, summary, status, cached_at "
        "FROM file_cache ORDER BY cached_at DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["keywords"] = json.loads(d.get("keywords") or "[]")
        except Exception:
            d["keywords"] = []
        out.append(d)
    return out
