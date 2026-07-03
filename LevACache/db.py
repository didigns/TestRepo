"""SQLite 캐시 저장소. 파일별 요약/키워드를 증분(해시) 기반으로 캐싱.

의미검색용 벡터는 vectors.py(chunk_vectors 테이블)가 담당한다.
여기는 메타데이터·키워드·요약·OCR 본문·진행 단계만 관리한다.
"""
import json
import os
import sqlite3
from datetime import datetime

# 키워드가 비어도 재분석을 반복하지 않기 위한 최대 재시도 횟수
MAX_KW_RETRY = 2

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
    ocr_text     TEXT,        -- 비전 OCR 본문(해시 변경 시 무효화)
    status       TEXT,        -- PENDING | CACHED | FAILED
    stage        INTEGER,     -- 진행 단계: 0 대기 · 1 본문준비 · 2 임베딩 · 3 키워드
    kw_retry     INTEGER DEFAULT 0,  -- 키워드가 빈 채 CACHED 된 횟수(무한 재분석 방지)
    error_msg    TEXT,
    cached_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_cache_hash ON file_cache(content_hash);
CREATE INDEX IF NOT EXISTS idx_cache_status ON file_cache(status);
"""


def connect(db_path):
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    # check_same_thread=False: agent.conn 은 메인 스레드에서 만들어져
    # 중량(heavy) 스레드에서만 사용된다. 연결마다 사용 스레드는 하나뿐이므로
    # 동시 사용은 없고, 생성-사용 스레드 불일치 검사만 끈다.
    conn = sqlite3.connect(db_path, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL 모드: 워커가 캐싱하며 DB에 쓰는 동안에도 앱(설정창)이 목록을 읽을 수 있게
    # 한다(리더-라이터 비차단). rollback journal 이면 쓰기 중 읽기가 잠겨 목록이 빈다.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate(conn)  # 기존 DB에 없는 컬럼 추가
    return conn


def _migrate(conn):
    """기존 DB 마이그레이션: 스키마에 추가된 컬럼을 채운다."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(file_cache)").fetchall()]
    if "stage" not in cols:
        conn.execute("ALTER TABLE file_cache ADD COLUMN stage INTEGER")
        # 기존 CACHED 항목은 완료(3)로 간주
        conn.execute("UPDATE file_cache SET stage=3 WHERE status='CACHED'")
    if "ocr_text" not in cols:
        conn.execute("ALTER TABLE file_cache ADD COLUMN ocr_text TEXT")
    if "kw_retry" not in cols:
        conn.execute("ALTER TABLE file_cache ADD COLUMN kw_retry INTEGER DEFAULT 0")
    conn.commit()


def get_entry(conn, path):
    row = conn.execute("SELECT * FROM file_cache WHERE path = ?", (path,)).fetchone()
    return dict(row) if row else None


def needs_caching(conn, path, content_hash):
    """캐시가 없거나 해시가 다르거나 CACHED가 아니면 True.
    CACHED인데 키워드가 비었으면(과거 파싱 실패) 재시도하되,
    kw_retry 가 MAX_KW_RETRY 에 도달하면 더 이상 반복하지 않는다."""
    row = get_entry(conn, path)
    if row is None:
        return True
    if row.get("content_hash") != content_hash:
        return True
    if row.get("status") != "CACHED":
        return True
    kw = (row.get("keywords") or "").strip()
    if kw in ("", "[]", "null"):
        return int(row.get("kw_retry") or 0) < MAX_KW_RETRY
    return False


def upsert_pending(conn, *, path, filename, ext, size_bytes, mtime, content_hash,
                   model_key, model_name):
    """분석 시작 전 PENDING 으로 등록/갱신.
    해시가 바뀐 파일은 ocr_text(구본문)와 kw_retry 를 초기화하고,
    해시가 같으면(키워드 재시도 등) 둘 다 보존해 OCR 재실행을 피한다."""
    conn.execute(
        """INSERT INTO file_cache(path, filename, ext, size_bytes, mtime, content_hash,
                                  model_key, model_name, status, stage, cached_at)
           VALUES(?,?,?,?,?,?,?,?, 'PENDING', 0, ?)
           ON CONFLICT(path) DO UPDATE SET
               filename=excluded.filename, ext=excluded.ext, size_bytes=excluded.size_bytes,
               mtime=excluded.mtime,
               ocr_text=CASE WHEN file_cache.content_hash=excluded.content_hash
                             THEN file_cache.ocr_text ELSE NULL END,
               kw_retry=CASE WHEN file_cache.content_hash=excluded.content_hash
                             THEN file_cache.kw_retry ELSE 0 END,
               content_hash=excluded.content_hash,
               model_key=excluded.model_key, model_name=excluded.model_name,
               status='PENDING', stage=0, error_msg=NULL""",
        (path, filename, ext, size_bytes, mtime, content_hash, model_key, model_name,
         datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def set_cached(conn, path, keywords, summary):
    """분석 완료. 키워드가 비어 있으면 kw_retry 를 올려
    같은 내용에 대한 무한 재분석을 막는다(성공 시 0으로 리셋)."""
    empty = not keywords
    conn.execute(
        """UPDATE file_cache SET keywords=?, summary=?, status='CACHED', stage=3,
               kw_retry=CASE WHEN ? THEN COALESCE(kw_retry,0)+1 ELSE 0 END,
               error_msg=NULL, cached_at=?
           WHERE path=?""",
        (json.dumps(keywords or [], ensure_ascii=False), summary, int(empty),
         datetime.now().isoformat(timespec="seconds"), path),
    )
    conn.commit()


def register_discovered(conn, items):
    """스캔 초기 수집(1차 gathering) 단계: 발견된 파일을 PENDING(stage 0)로 미리 등록.
    이미 존재하는 행은 절대 건드리지 않는다(완료/진행 상태·해시 보존).
    items: [{"path","filename","ext","model_key"}, ...] → 새로 등록된 건수 반환."""
    now = datetime.now().isoformat(timespec="seconds")
    added = 0
    for it in items:
        cur = conn.execute(
            """INSERT INTO file_cache(path, filename, ext, model_key, status, stage, cached_at)
               VALUES(?,?,?,?, 'PENDING', 0, ?)
               ON CONFLICT(path) DO NOTHING""",
            (it["path"], it.get("filename"), it.get("ext"),
             it.get("model_key"), now),
        )
        added += cur.rowcount
    conn.commit()
    return added


def set_stage(conn, path, stage):
    """진행 단계를 갱신(1 본문준비 · 2 임베딩 · 3 키워드). status는 건드리지 않음."""
    conn.execute("UPDATE file_cache SET stage=? WHERE path=?", (int(stage), path))
    conn.commit()


def set_failed(conn, path, error_msg):
    conn.execute(
        "UPDATE file_cache SET status='FAILED', error_msg=?, cached_at=? WHERE path=?",
        (str(error_msg)[:500], datetime.now().isoformat(timespec="seconds"), path),
    )
    conn.commit()


def get_ocr_text(conn, path):
    """저장된 OCR 본문. upsert_pending 이 해시 변경 시 지우므로
    값이 있으면 현재 파일 내용과 일치한다."""
    row = conn.execute(
        "SELECT ocr_text FROM file_cache WHERE path=?", (path,)
    ).fetchone()
    return (row["ocr_text"] or "") if row else ""


def set_ocr_text(conn, path, text):
    conn.execute("UPDATE file_cache SET ocr_text=? WHERE path=?", (text or "", path))
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


def _prefix_len(a, b):
    """두 문자열의 공통 접두어 길이."""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def search(conn, query, limit=5):
    """질문과 관련된 캐시 엔트리를 검색(파일명·키워드·요약 매칭 수로 랭킹).
    의미검색(vectors.search) 실패/미설정 시의 폴백."""
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
            elif any(_prefix_len(t, w) >= 2
                     and _prefix_len(t, w) >= 0.6 * min(len(t), len(w))
                     for w in hay_words):
                # 공통 접두어 매칭 — 조사·번호 차이 흡수
                # (예: 질문 '다크메이지가' ↔ 파일명 '다크메이지1.txt')
                score += 1
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


def list_entries(conn, limit=500):
    rows = conn.execute(
        "SELECT path, filename, ext, model_key, keywords, summary, status, stage, error_msg, cached_at "
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
