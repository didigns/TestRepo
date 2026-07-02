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
    """캐시가 없거나 해시가 다르거나 CACHED가 아니면 True.
    또한 CACHED여도 키워드가 비어있으면(과거 파싱 실패) 다시 캐싱한다."""
    row = get_entry(conn, path)
    if row is None:
        return True
    if row.get("content_hash") != content_hash:
        return True
    if row.get("status") != "CACHED":
        return True
    kw = (row.get("keywords") or "").strip()
    if kw in ("", "[]", "null"):
        return True
    return False


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


def clear_all(conn):
    """캐시 DB의 모든 엔트리를 삭제(초기화)."""
    conn.execute("DELETE FROM file_cache")
    conn.commit()


def remove_under(conn, folder):
    """폴더 자신과 그 하위의 모든 캐시 엔트리를 제거. 삭제된 행 수 반환."""
    folder = os.path.normpath(folder)
    like = folder + os.sep + "%"
    cur = conn.execute(
        "DELETE FROM file_cache WHERE path = ? OR path LIKE ?", (folder, like)
    )
    conn.commit()
    return cur.rowcount


def search(conn, query, limit=5):
    """질문과 관련된 캐시 엔트리를 검색(파일명·키워드·요약 매칭 수로 랭킹)."""
    import re
    q = (query or "").lower()
    tokens = [t for t in re.split(r"[\s,./\\()\[\]{}!?~\-_:;\"']+", q) if len(t) >= 2]
    if not tokens:
        return []
    rows = conn.execute(
        "SELECT filename, path, keywords, summary FROM file_cache WHERE status='CACHED'"
    ).fetchall()
    scored = []
    for r in rows:
        hay = " ".join([
            r["filename"] or "", r["keywords"] or "", r["summary"] or ""
        ]).lower()
        hay_words = hay.split()
        score = 0
        for t in tokens:
            if t in hay:
                score += 2  # 정확 부분일치
            elif any(t in w or w in t for w in hay_words):
                score += 1  # 한국어 복합어 등 양방향 부분일치
        if score:
            scored.append((score, dict(r)))
    scored.sort(key=lambda x: -x[0])
    out = []
    for _score, r in scored[:limit]:
        try:
            r["keywords"] = json.loads(r.get("keywords") or "[]")
        except Exception:
            r["keywords"] = []
        out.append(r)
    return out


def list_entries(conn, limit=200):
    rows = conn.execute(
        "SELECT path, filename, ext, model_key, keywords, summary, status, error_msg, cached_at "
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
