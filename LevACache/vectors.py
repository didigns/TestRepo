"""청크 단위 임베딩 저장/검색.

파일을 조각(청크)으로 나눠 각 조각을 임베딩하고, 질문 벡터와 코사인 유사도로
검색한 뒤 파일 단위로 집계(최고 청크 점수)한다. 파일에 청크 벡터가 하나라도
있으면 즉시 검색되므로, '채팅은 1단계(임베딩) 이후부터 가능'이 자연히 성립한다.

저장은 별도 테이블 chunk_vectors 를 쓴다(file_cache 와 분리).
벡터는 정규화된 float32 BLOB(코사인 = 내적).
"""
import array as _array
import math as _math

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunk_vectors (
    path        TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    text        TEXT,
    embedding   BLOB NOT NULL,
    PRIMARY KEY (path, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_chunkvec_path ON chunk_vectors(path);
"""


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def _normalize(vec):
    n = _math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def set_file_chunks(conn, path, chunks, vectors):
    """파일의 청크 벡터를 교체(기존 삭제 후 삽입). chunks/vectors 길이 동일.
    text 는 청크 전문을 저장한다 — RAG 컨텍스트로 직접 주입되므로
    잘라 두면(과거 500자 캡) 답변 품질이 떨어진다."""
    conn.execute("DELETE FROM chunk_vectors WHERE path=?", (path,))
    rows = []
    for i, (text, vec) in enumerate(zip(chunks, vectors)):
        if not vec:
            continue
        buf = _array.array("f", _normalize(vec)).tobytes()
        rows.append((path, i, text or "", buf))
    conn.executemany(
        "INSERT INTO chunk_vectors(path, chunk_index, text, embedding) VALUES(?,?,?,?)",
        rows,
    )
    conn.commit()
    return len(rows)


def delete_file(conn, path):
    conn.execute("DELETE FROM chunk_vectors WHERE path=?", (path,))
    conn.commit()


def clear_all(conn):
    """모든 청크 벡터 삭제(캐시 초기화용). file_cache 초기화와 함께 호출해야
    삭제된 파일의 벡터가 의미검색에 유령처럼 남지 않는다."""
    conn.execute("DELETE FROM chunk_vectors")
    conn.commit()


def delete_under(conn, folder, sep="/"):
    like = folder.rstrip(sep) + sep + "%"
    cur = conn.execute(
        "DELETE FROM chunk_vectors WHERE path=? OR path LIKE ?", (folder, like)
    )
    conn.commit()
    return cur.rowcount


def has_vectors(conn, path):
    r = conn.execute(
        "SELECT 1 FROM chunk_vectors WHERE path=? LIMIT 1", (path,)
    ).fetchone()
    return r is not None


def _load_all(conn):
    rows = conn.execute(
        "SELECT path, chunk_index, text, embedding FROM chunk_vectors"
    ).fetchall()
    out = []
    for r in rows:
        v = _array.array("f")
        v.frombytes(r[3] if not hasattr(r, "keys") else r["embedding"])
        path = r[0] if not hasattr(r, "keys") else r["path"]
        idx = r[1] if not hasattr(r, "keys") else r["chunk_index"]
        text = r[2] if not hasattr(r, "keys") else r["text"]
        out.append((path, idx, text, list(v)))
    return out


def search(conn, query_vec, *, limit=5, min_score=0.25):
    """질문 벡터로 청크 코사인 검색 후 파일 단위 집계(최고 청크 점수).
    반환: [{path, score, snippet, chunk_index}] score 내림차순.
    chunk_index 는 매칭 청크 위치 — 이웃 청크와 함께 컨텍스트로 주입된다.
    """
    items = _load_all(conn)
    if not items:
        return []
    q = _normalize(query_vec)
    try:
        import numpy as np
        mat = np.array([v for _p, _i, _t, v in items], dtype=np.float32)
        qv = np.array(q, dtype=np.float32)
        scores = mat @ qv
        pairs = [(float(scores[i]), items[i][0], items[i][1], items[i][2])
                 for i in range(len(items))]
    except Exception:
        pairs = []
        for path, idx, text, v in items:
            s = sum(a * b for a, b in zip(q, v))
            pairs.append((s, path, idx, text))
    # 파일 단위 집계: 최고 청크 점수 + 그 청크 위치/스니펫
    best = {}
    for score, path, idx, text in pairs:
        if path not in best or score > best[path][0]:
            best[path] = (score, idx, text)
    ranked = sorted(best.items(), key=lambda kv: -kv[1][0])
    out = []
    for path, (score, idx, text) in ranked:
        if score < min_score:
            break
        out.append({"path": path, "score": round(float(score), 4),
                    "snippet": text or "", "chunk_index": int(idx)})
        if len(out) >= limit:
            break
    return out


def get_context(conn, path, chunk_index, *, before=1, after=1, max_chars=4000):
    """매칭 청크와 앞뒤 이웃 청크를 이어붙여 문맥을 만든다.

    파일 '앞부분'이 아니라 질문과 실제로 관련된 부위를 돌려주므로
    긴 문서(소설 등)에서도 RAG 컨텍스트가 유효하다. 무중첩 분할로
    문장이 청크 경계에 걸린 경우도 이웃 포함으로 복원된다."""
    lo, hi = int(chunk_index) - int(before), int(chunk_index) + int(after)
    rows = conn.execute(
        "SELECT chunk_index, text FROM chunk_vectors"
        " WHERE path=? AND chunk_index BETWEEN ? AND ? ORDER BY chunk_index",
        (path, lo, hi),
    ).fetchall()
    parts = [(r[1] if not hasattr(r, "keys") else r["text"]) or "" for r in rows]
    joined = "".join(parts)
    if len(joined) <= max_chars:
        return joined
    # 예산 초과 시 매칭 청크가 가운데 오도록 잘라낸다
    center = 0
    for r, p in zip(rows, parts):
        ci = r[0] if not hasattr(r, "keys") else r["chunk_index"]
        if ci == int(chunk_index):
            center += len(p) // 2
            break
        center += len(p)
    half = max_chars // 2
    start = max(0, min(center - half, len(joined) - max_chars))
    return joined[start:start + max_chars]
