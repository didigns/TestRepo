"""LevACache 워커.

실행 로직은 stages.Pipeline(3단계: 본문준비 → 임베딩 → 키워드)이 담당하고,
이 모듈은 Pipeline 에 의존성(ctx)을 공급하는 어댑터(CacheAgent)와
프로세스 진입점(NDJSON 데몬/CLI)을 제공한다.

모드:
  --cache <파일...>   지정 파일 캐싱 후 종료
  --scan <폴더...>    폴더 내 지원 파일 중 해시가 바뀐 것만 캐싱
  --daemon            stdin NDJSON 명령 수신(부모=Electron).
                      중량 명령(cache/cache-batch/scan)은 전용 스레드 큐로 순차 처리,
                      경량 명령(list/gather/clear/uncache/prune)은 stdin 스레드가 즉시 응답,
                      chat 은 우선 큐 — 캐싱 중에도 파일/조각 경계에서 먼저 처리
                      명령: {"id","type":"cache","path"} / {"type":"cache-batch","paths":[...]} /
                            {"type":"uncache","path","prefix"} / {"type":"prune"} /
                            {"type":"clear"} / {"type":"gather","paths":[...]} /
                            {"type":"scan","paths":[...]} / {"type":"chat","prompt"} /
                            {"type":"list"} / {"type":"shutdown"}
                      출력: {"type":"ready"} / {"type":"progress"...} /
                            {"type":"result","id",...} / {"type":"cache-event"...}

옵션: --config <leva-config.json> --db <경로>
"""
import argparse
import json
import os
import queue
import re
import sys
import threading
import time

from . import db as dbmod
from . import extract, router, summarize, vectors
from .config import load_config
from .llama_manager import LlamaManager
from .stages import Pipeline
from .whisper_manager import WhisperManager


# 경량 스레드(stdin)와 중량 스레드(scan 등)가 동시에 emit 하므로
# 줄이 섞이지 않게 stdout 쓰기를 락으로 직렬화한다.
_EMIT_LOCK = threading.Lock()

# Windows Job Object 핸들. 프로세스 수명 동안 절대 닫지 않는다
# (닫히는 순간 = 워커 종료 시점에 OS가 잡 안의 자식들을 모두 종료).
_JOB_HANDLE = None


def _install_kill_on_close_job():
    """자신과 모든 자식(llama-server.exe 등)을 Windows Job Object로 묶는다.

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE 덕분에 이 워커가 어떤 이유로든
    죽으면(강제 종료·크래시 포함) OS가 잡 핸들을 닫으며 llama-server를
    함께 종료시킨다. finally/시그널 핸들러에 의존하지 않는 확실한 정리.
    """
    global _JOB_HANDLE
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class _BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class _ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimits),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9

        job = k32.CreateJobObjectW(None, None)
        if not job:
            return
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = k32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        )
        if ok:
            # 자신을 잡에 넣으면 이후 생성되는 자식(llama-server)이 자동 편입된다.
            ok = k32.AssignProcessToJobObject(job, k32.GetCurrentProcess())
        if not ok:
            k32.CloseHandle(job)
            return
        _JOB_HANDLE = job
    except Exception:
        pass  # 실패해도 워커 기능 자체는 정상 동작(자동 정리만 보장 안 됨)


def _emit(msg):
    line = json.dumps(msg, ensure_ascii=False) + "\n"
    with _EMIT_LOCK:
        sys.stdout.write(line)
        sys.stdout.flush()


def _log(msg):
    sys.stderr.write(str(msg) + "\n")
    sys.stderr.flush()


# 대화용 고정 system 프롬프트. 매 요청 동일하므로 llama-server가 이 프리픽스의
# KV 캐시를 재사용해 첫 토큰 지연(TTFT)을 줄인다.
CHAT_SYSTEM = (
    "당신은 LevA, 사용자의 로컬 파일을 돕는 한국어 비서입니다. "
    "간결하고 정확하게 답하세요. "
    "마크다운 문법(**, ##, * 등)이나 LaTeX($...$, \\rightarrow 등)를 쓰지 말고 "
    "일반 문장과 줄바꿈으로만 답하세요. 화살표가 필요하면 → 기호를 그대로 쓰세요. "
    "제공된 파일 내용을 참고했다면, 반드시 답변 맨 끝 줄에 참고한 파일명을 "
    "[파일명.확장자] 형식으로 대괄호에 하나씩 넣어 표기하세요. "
    "예: [보고서.pdf] [회의 07.02.txt]"
)


class CacheAgent:
    """Pipeline 의 ctx 구현 + 검색/채팅(RAG) 담당."""

    def __init__(self, cfg, conn):
        self.cfg = cfg
        self.conn = conn
        self.llama = LlamaManager(cfg, logger=_log)
        self.whisper = WhisperManager(cfg, logger=_log)  # 음성(STT) 전용 서버
        vectors.ensure_schema(conn)  # 청크 벡터 테이블 준비
        self.pipeline = Pipeline(self)
        # 대화 우선 큐: 캐싱(스캔) 중에 chat이 오면 파이프라인이
        # 파일/조각 경계(checkpoint)에서 이 큐를 먼저 비운다.
        self.chat_q = queue.Queue()
        # 취소 플래그: DB 초기화(clear) 시 설정돼 진행 중인 배치가
        # 파일/조각 경계에서 즉시 중단된다. 새 중량 명령 시작 시 해제.
        self.cancel_ev = threading.Event()
        # 일시정지 플래그: 파일/조각 경계에서 캐싱을 멈춰 리소스를 양보한다.
        # (대화는 일시정지 중에도 계속 처리된다)
        self.pause_ev = threading.Event()
        # 멀티턴 대화 히스토리: (질문, 답변) 최근 6턴 — 후속 질문 맥락용
        self.chat_history = []

    def close(self):
        self.llama.stop()
        self.whisper.stop()

    def _pause_wait(self):
        """일시정지 동안 대기. 대화(chat)는 계속 서비스한다."""
        while self.pause_ev.is_set() and not self.cancel_ev.is_set():
            self.service_chats()
            time.sleep(0.25)

    # ================= Pipeline ctx: 파일/라우팅 =======================
    @staticmethod
    def isfile(path):
        return os.path.isfile(path)

    @staticmethod
    def basename(path):
        return os.path.basename(path)

    @staticmethod
    def size(path):
        return os.stat(path).st_size

    @staticmethod
    def route(path):
        return router.route(path)

    @staticmethod
    def file_hash(path):
        return extract.file_hash(path)

    def prepare(self, path, ext):
        # pdfMaxPages 0 = 전체 페이지 (스캔 PDF도 문서 전체를 OCR)
        return extract.prepare(path, ext, pdf_max_pages=self.cfg.get("pdfMaxPages", 0))

    # ================= Pipeline ctx: 모델 ==============================
    def ensure_vision(self):
        self.llama.ensure_model("vision")

    def ensure_text(self):
        self.llama.ensure_model("text")

    def transcribe(self, path):
        """음성 파일 → 타임스탬프 있는 전사 본문(Whisper). Pipeline ctx 훅."""
        return self.whisper.transcribe(path)

    def ocr(self, images, name):
        """비전 모델로 이미지의 글자·내용을 텍스트로 추출.

        여러 페이지(스캔 PDF 전체)는 페이지당 1요청으로 나눠 처리한다 —
        한 요청에 이미지를 몰아넣으면 비전 컨텍스트가 초과되고,
        나눠야 페이지별 진행 표시·일시정지·취소도 가능하다."""
        images = list(images or [])
        total = len(images)
        texts = []
        for i, img in enumerate(images, 1):
            if self.cancel_ev.is_set():
                raise RuntimeError("캐싱 취소됨(DB 초기화)")
            self._pause_wait()
            if total > 1:
                self.emit({"phase": "progress", "path": "", "filename": name,
                           "step": f"글자 추출 중 (MiniCPM) — 페이지 {i}/{total}",
                           "stage": 1})
            t = self.llama.chat(summarize.build_ocr_prompt(name), images=[img]) or ""
            if t.strip():
                texts.append(t.strip())
        return "\n\n".join(texts)

    def _embed_split(self, text):
        """임베딩용 청크 분할. 검색 정확도를 위해 분석용보다 촘촘하게.
        embedMaxChunks 가 0(기본)이면 개수 제한 없이 본문 전체를 청크로 만든다."""
        size = int(self.cfg.get("embedChunkChars", 1000) or 1000)
        max_parts = int(self.cfg.get("embedMaxChunks", 0) or 0)  # 0 = 무제한
        chunks = summarize.split_body(text or "", size)
        return chunks[:max_parts] if max_parts > 0 else chunks

    def embed_store(self, path, text, fallback_name):
        """본문을 청크로 임베딩해 저장. 임베딩 모델 미설정 시 0.
        text가 비면 파일명만이라도 임베딩(최소 검색 가능)."""
        if not self.cfg.get("embedModel"):
            return 0
        chunks = self._embed_split(text) or [fallback_name]
        vecs = self.llama.embed(chunks)
        if not vecs:
            return 0
        return vectors.set_file_chunks(self.conn, path, chunks, vecs)

    def run_keywords(self, fname, body, path=None):
        """본문을 조각내어 반복(loop) 추출. 조각마다 진행상황을 알린다."""
        chunk_size = int(self.cfg.get("chunkChars", 2000) or 2000)
        max_parts = int(self.cfg.get("maxChunks", 12) or 12)

        def on_progress(i, n):
            # 취소(DB 초기화) 요청이면 조각 경계에서 즉시 중단한다.
            # 예외는 배치 루프가 잡고, clear 후라 set_failed는 no-op(UPDATE).
            if self.cancel_ev.is_set():
                raise RuntimeError("캐싱 취소됨(DB 초기화)")
            self._pause_wait()  # 일시정지면 조각 경계에서 멈춘다
            # 조각 하나 끝날 때마다 대기 중인 대화를 먼저 처리한다
            # (3단계는 text 모델이 이미 로드돼 있어 스왑 비용 없음).
            self.service_chats()
            label = "내용 분석 중 (Gemma)"
            if n > 1:
                label += f" — 조각 {i}/{n}"
            self.emit({"phase": "progress", "path": path or "", "filename": fname,
                       "step": label, "stage": 3})

        # 회의 녹음(전사본)은 안건·결정·액션아이템 중심의 회의록으로 정리
        ext = os.path.splitext(path or fname or "")[1].lower()
        mode = "meeting" if ext in router.AUDIO_EXTS else "doc"
        return summarize.run_text_extraction(
            self.llama.chat, fname, body,
            chunk_size=chunk_size, max_parts=max_parts, on_progress=on_progress,
            mode=mode,
        )

    # ================= Pipeline ctx: 저장소 ============================
    def needs_caching(self, path, content_hash):
        return dbmod.needs_caching(self.conn, path, content_hash)

    def upsert_pending(self, path, *, ext, model_key, content_hash):
        st = os.stat(path)
        model_cfg_key = {"vision": "visionModel", "audio": "whisperModel"}.get(
            model_key, "textModel")
        model_name = os.path.basename(self.cfg.get(model_cfg_key) or "")
        dbmod.upsert_pending(
            self.conn, path=path, filename=os.path.basename(path), ext=ext,
            size_bytes=st.st_size, mtime=str(int(st.st_mtime)),
            content_hash=content_hash, model_key=model_key, model_name=model_name,
        )

    def get_ocr_text(self, path):
        return dbmod.get_ocr_text(self.conn, path)

    def set_ocr_text(self, path, text):
        dbmod.set_ocr_text(self.conn, path, text)

    def set_stage(self, path, stage):
        dbmod.set_stage(self.conn, path, stage)

    def set_cached(self, path, keywords, summary):
        dbmod.set_cached(self.conn, path, keywords, summary)

    def set_failed(self, path, error):
        dbmod.set_failed(self.conn, path, error)

    def remove(self, path):
        dbmod.remove(self.conn, path)

    # ================= Pipeline ctx: 알림 ==============================
    @staticmethod
    def emit(ev):
        _emit({"type": "cache-event", **ev})

    @staticmethod
    def log(msg):
        _log(msg)

    # ================= 캐싱 진입점 =====================================
    def cache_file(self, path, *, force=False):
        """한 파일을 캐싱(깊이 우선 1→2→3). 반환: dict(status, ...)."""
        return self.pipeline.process_one(os.path.abspath(path), force=force)

    def cache_batch(self, paths, *, force=False):
        """여러 파일을 단계 우선(1→1→…→2→…→3→…)으로 캐싱.
        파일 이벤트 마이크로 배칭용 — 모델 스왑이 배치당 최대 1회."""
        ordered = self._order_paths(
            [os.path.abspath(p) for p in paths or [] if p]
        )
        ordered = [p for p in ordered if os.path.isfile(p)]
        # 분석 전에 목록(PENDING)에 먼저 등록 + discovered 알림
        # → 설정창 목록에 새 파일이 즉시 보이고 HUD가 시작을 알 수 있다
        if ordered:
            self._register_ordered("(파일 이벤트)", ordered)
        results = {"cached": 0, "unchanged": 0, "failed": 0, "skipped": 0}
        self.pipeline.process_many(ordered, results, force=force)
        return results

    @staticmethod
    def _order_paths(paths):
        """텍스트류 먼저, 이미지류(vision)는 뒤로 정렬해 스왑을 줄인다."""
        text_like, image_like = [], []
        for p in paths:
            ext = os.path.splitext(p)[1].lower()
            (image_like if ext in router.IMAGE_EXTS else text_like).append(p)
        return text_like + image_like

    def _collect_supported(self, folder):
        """폴더를 walk 하여 지원 파일 경로를 수집(텍스트 먼저, 이미지 뒤)."""
        found = []
        for root, _dirs, files in os.walk(folder):
            for fn in files:
                p = os.path.join(root, fn)
                if router.is_supported(p):
                    found.append(os.path.abspath(p))
        return self._order_paths(found)

    def _register_ordered(self, folder, ordered, conn=None):
        """수집된 파일 전체를 대기(PENDING·0단계)로 목록에 미리 등록하고
        'discovered' 이벤트를 보낸다(분석 전에 전체 리스트가 보이도록).
        conn: 스레드별 연결 주입용(기본은 self.conn — 생성 스레드 전용)."""
        conn = conn if conn is not None else self.conn
        try:
            items = []
            for p in ordered:
                mk, ext = router.route(p)
                items.append({
                    "path": p, "filename": os.path.basename(p),
                    "ext": ext, "model_key": mk,
                })
            added = dbmod.register_discovered(conn, items)
            _emit({"type": "cache-event", "phase": "discovered",
                   "folder": folder, "count": len(ordered), "added": added})
            return added
        except Exception as e:
            _log("사전 등록 실패: " + str(e))
            return 0

    def gather_folders(self, folders, conn=None):
        """수집(gathering) 전용: 여러 폴더의 파일을 모아 전부 대기 상태로 등록만 한다.
        분석(모델 실행)은 하지 않으므로 빠르며, 목록을 먼저 채우는 용도다.
        conn: 경량 스레드에서 호출할 때 그 스레드의 연결을 주입한다."""
        total = 0
        for folder in folders or []:
            ordered = self._collect_supported(folder)
            self._register_ordered(folder, ordered, conn=conn)
            total += len(ordered)
        _emit({"type": "cache-event", "phase": "gather-done", "count": total})
        return total

    def scan_folder(self, folder):
        results = {"discovered": 0, "cached": 0, "unchanged": 0, "failed": 0, "skipped": 0}
        ordered = self._collect_supported(folder)
        results["discovered"] = len(ordered)
        # 수집 즉시 전 파일을 대기로 등록(이미 gather로 등록됐다면 멱등, no-op)
        self._register_ordered(folder, ordered)
        # 단계 우선(breadth-first) 처리 — 모델 스왑 최소화
        self.pipeline.process_many(ordered, results)
        return results

    def scan_folders(self, folders):
        """여러 폴더를 '하나의 배치'로 스캔한다.

        폴더별로 scan_folder 를 따로 돌리면 첫 폴더의 3단계(가장 느림)가
        끝날 때까지 다른 폴더 파일들이 0단계 대기로 남는다. 전 폴더 파일을
        모아 한 배치로 돌리면 '모든 파일 1→2단계 완료 후에만 3단계 시작'이
        폴더 경계와 무관하게 보장된다(2단계까지 끝나면 전 파일 검색 가능)."""
        results = {"discovered": 0, "cached": 0, "unchanged": 0, "failed": 0, "skipped": 0}
        all_paths = []
        for folder in folders or []:
            ordered = self._collect_supported(folder)
            self._register_ordered(folder, ordered)
            all_paths.extend(ordered)
        all_paths = self._order_paths(all_paths)  # 폴더 경계 없이 텍스트 먼저
        results["discovered"] = len(all_paths)
        self.pipeline.process_many(all_paths, results)
        return results

    def uncache(self, path, *, prefix=False, conn=None):
        """삭제/이동된 파일(또는 폴더)의 캐시 엔트리를 제거."""
        conn = conn if conn is not None else self.conn
        path = os.path.abspath(path)
        if prefix:
            removed = dbmod.remove_under(conn, path)
            vectors.delete_under(conn, path, sep=os.sep)
        else:
            dbmod.remove(conn, path)
            vectors.delete_file(conn, path)
            removed = 1
        return {"ok": True, "path": path, "removed": removed}

    def clear_all(self, conn=None):
        """캐시 초기화: 메타(file_cache)와 청크 벡터(chunk_vectors)를 함께 삭제."""
        conn = conn if conn is not None else self.conn
        dbmod.clear_all(conn)
        vectors.clear_all(conn)

    def prune(self, conn=None):
        """디스크에 더 이상 존재하지 않는 파일의 캐시 엔트리를 모두 제거."""
        conn = conn if conn is not None else self.conn
        removed = 0
        paths = [row[0] for row in
                 conn.execute("SELECT path FROM file_cache").fetchall()]
        for p in paths:
            if not os.path.isfile(p):
                dbmod.remove(conn, p)
                vectors.delete_file(conn, p)
                removed += 1
        return removed

    # ================= 검색/채팅(RAG) ==================================
    # 전역 질문(전체 요약 등) — 청크 몇 개가 아니라 파일 전체 조망이 필요한 질문
    _GLOBAL_QUERY_RE = re.compile(
        r"요약|줄거리|스토리|전체|전반|주제|개요|정리\s*해|무슨\s*내용|어떤\s*내용|소개\s*해")

    @staticmethod
    def _fname_tokens(filename):
        """파일명에서 의미 토큰 추출(숫자·기호·확장자 제거, 2자 이상)."""
        stem = os.path.splitext(filename or "")[0]
        return [t for t in re.split(r"[\s\d_\-\.\,\(\)\[\]〔〕『』]+", stem)
                if len(t) >= 2]

    def _name_match_score(self, prompt, filename):
        """질문에 파일명 토큰이 등장하는 개수(부분일치 포함).
        예: 질문 '2차 멘토링 회의내용' ↔ 파일 '멘토링2회차 녹음 내용.pdf'
        → 토큰 [멘토링, 회차, 녹음, 내용] 중 '멘토링','내용' 매칭 = 2."""
        q = (prompt or "").lower()
        return sum(1 for t in self._fname_tokens(filename) if t.lower() in q)

    def _retrieve(self, prompt, limit=2, conn=None):
        """질문과 관련된 파일 검색. 청크 임베딩 우선(1단계만 돼도 검색됨),
        실패 시 키워드 토큰검색으로 폴백.

        의미검색 점수는 '질문 ↔ 본문 청크' 유사도라 파일명 언급을 반영하지
        못한다(예: '2차 멘토링' 질문이 '멘토링2회차 녹음.pdf'를 못 이김).
        후보를 넉넉히 받아 파일명 토큰 매칭 보너스로 재랭킹한다.
        conn: 호출 스레드의 DB 연결(기본: 중량 스레드용 self.conn)."""
        conn = conn if conn is not None else self.conn
        if self.cfg.get("embedModel"):
            try:
                qv = self.llama.embed([prompt])
                if qv:
                    vhits = vectors.search(
                        conn, qv[0], limit=max(limit * 4, 8),
                        min_score=float(self.cfg.get("embedMinScore", 0.25)),
                    )
                    for vh in vhits:
                        bonus = self._name_match_score(
                            prompt, os.path.basename(vh["path"]))
                        if bonus:
                            vh["score"] = round(
                                vh["score"] + 0.2 * min(bonus, 2), 4)
                    vhits.sort(key=lambda h: -h["score"])
                    vhits = vhits[:limit]
                    out = []
                    for vh in vhits:
                        row = dbmod.get_entry(conn, vh["path"]) or {}
                        try:
                            kws = json.loads(row.get("keywords") or "[]")
                        except Exception:
                            kws = []
                        out.append({
                            "path": vh["path"],
                            "filename": row.get("filename") or os.path.basename(vh["path"]),
                            "summary": row.get("summary") or "",
                            "keywords": kws,
                            "score": vh.get("score"),
                            "snippet": vh.get("snippet", ""),
                            "chunk_index": vh.get("chunk_index"),
                        })
                    if out:
                        _log("의미검색 히트: " + ", ".join(
                            f"{o['filename']}#{o.get('chunk_index')}(score {o.get('score')})"
                            for o in out))
                        return out
                    _log(f"의미검색 0건(min_score {self.cfg.get('embedMinScore', 0.25)})"
                         " → 토큰검색 폴백")
            except Exception as e:
                _log("의미검색 실패, 토큰검색으로 폴백: " + str(e))
        return dbmod.search(conn, prompt, limit=limit)

    def _read_body(self, path):
        """채팅 컨텍스트용 본문. 텍스트는 재추출, 이미지·스캔PDF는 저장된 OCR 사용."""
        try:
            if not os.path.isfile(path):
                return ""
            _key, ext = router.route(path)
            prep = self.prepare(path, ext)
            body = (prep.get("text") or "").strip()
            if body:
                return body
        except Exception:
            pass
        # 텍스트가 없으면(이미지/스캔본) 캐싱 때 저장한 OCR 본문으로 대체
        try:
            return (dbmod.get_ocr_text(self.conn, path) or "").strip()
        except Exception:
            return ""

    def _match_files_by_name(self, prompt, limit=4):
        """질문에 파일명 토큰이 등장하는 캐시 파일(상태 무관).
        매칭 토큰 수 내림차순, 동점이면 파일명순(다크메이지 1→4 권 순서)."""
        rows = self.conn.execute(
            "SELECT path, filename, summary, keywords FROM file_cache"
        ).fetchall()
        scored = []
        for r in rows:
            s = self._name_match_score(prompt, r["filename"])
            if s:
                scored.append((s, dict(r)))
        scored.sort(key=lambda x: (-x[0], x[1]["filename"]))
        return [d for _s, d in scored[:limit]]

    def _sample_chunk_excerpts(self, path, budget):
        """chunk_vectors에 저장된 청크 전문에서 문서 전체 균등 발췌.
        (재추출 없이 임베딩 때 저장한 텍스트 재사용 — 스캔PDF의 OCR도 포함)"""
        rows = self.conn.execute(
            "SELECT text FROM chunk_vectors WHERE path=? ORDER BY chunk_index",
            (path,),
        ).fetchall()
        chunks = [(r[0] or "") for r in rows]
        chunks = [c for c in chunks if c.strip()]
        if not chunks:
            return ""
        k = max(1, budget // 1000)  # 청크 ≈ embedChunkChars(1000자)
        picked = summarize.sample_chunks(chunks, k)
        out = "\n(…중략…)\n".join(picked)
        return out[:budget]

    def _augment_global(self, prompt):
        """전역 질문(전체 요약 등)용 컨텍스트: 청크 몇 개가 아니라
        '파일 요약 + 문서 전체 균등 발췌'를 파일별로 주입한다.
        대상 파일은 파일명 매칭 우선, 없으면 의미검색 히트."""
        q = self._retrieval_query(prompt)
        files = self._match_files_by_name(q)
        if not files:
            seen, files = set(), []
            for h in self._retrieve(q, limit=3):
                p = h.get("path", "")
                if p and p not in seen:
                    seen.add(p)
                    row = dbmod.get_entry(self.conn, p) or {}
                    files.append({
                        "path": p,
                        "filename": h.get("filename") or os.path.basename(p),
                        "summary": row.get("summary") or h.get("summary") or "",
                        "keywords": row.get("keywords"),
                    })
        if not files:
            return None, []
        budget = 7000
        per_file = max(1200, budget // len(files))
        blocks, refs = [], []
        for f in files:
            head = f"[파일: {f['filename']}]"
            summ = (f.get("summary") or "").strip()
            if summ:
                head += f"\n요약: {summ[:600]}"
            excerpt = self._sample_chunk_excerpts(f["path"], per_file)
            if not excerpt and not summ:
                excerpt = (self._read_body(f["path"]) or "")[:per_file]
            if excerpt:
                head += "\n발췌(문서 전체에서 균등 추출):\n" + excerpt
            blocks.append(head)
            refs.append({"filename": f["filename"], "path": f["path"],
                         "score": None, "chunk_index": None,
                         "snippet": (summ or excerpt or "")[:200],
                         "mode": "global"})
        _log("전역 질문 컨텍스트: " + ", ".join(r["filename"] for r in refs))
        context = (
            "아래는 사용자의 로컬 파일들의 요약과, 각 문서 전체에서 균등하게 "
            "발췌한 대목입니다. 발췌 사이 내용은 생략돼 있으니 이를 종합해 "
            "전체를 아우르는 답을 하세요. 참고한 파일명을 함께 언급하세요.\n\n"
            + "\n\n".join(blocks)
            + "\n\n질문: "
        )
        return context + prompt, refs

    def _augment(self, prompt):
        """관련 캐시 파일에서 '질문과 관련된 부위'를 프롬프트에 덧붙인다.

        의미검색 히트는 매칭 청크(+앞뒤 이웃)를 주입한다 — 파일 앞부분을
        넣으면 긴 문서(소설 등)에서 정작 관련 내용이 빠진다(다크메이지 사례).
        토큰검색 폴백 히트는 종전처럼 본문 앞부분을 쓴다."""
        # 전역 질문(전체 요약·줄거리 등)은 청크 검색이 구조적으로 부적합
        # → 요약+균등 발췌 경로로 라우팅
        if self._GLOBAL_QUERY_RE.search(prompt or ""):
            full, refs = self._augment_global(prompt)
            if full:
                return full, refs
        # 상위 2개만 주입(프롬프트를 짧게 유지해 프리필/TTFT 절감)
        hits = self._retrieve(self._retrieval_query(prompt), limit=2)
        full = prompt
        if hits:
            budget = 7000  # 컨텍스트에 넣을 총 본문 문자 예산
            blocks = []
            for h in hits:
                if budget <= 0:
                    break
                excerpt = ""
                ci = h.get("chunk_index")
                if ci is not None:
                    # 매칭 청크 ± 1 이웃 — 히트당 최대 4,000자로 제한해
                    # 두 번째 히트도 예산(≥3,000자)을 확보한다.
                    try:
                        excerpt = vectors.get_context(
                            self.conn, h["path"], ci,
                            before=1, after=1, max_chars=min(budget, 4000),
                        )
                    except Exception as e:
                        _log("청크 문맥 조회 실패: " + str(e))
                if not excerpt:
                    body = self._read_body(h.get("path", ""))
                    excerpt = (body or "")[:budget]
                if excerpt:
                    budget -= len(excerpt)
                    summ = (h.get("summary") or "").strip()
                    head = f"[파일: {h['filename']}]"
                    if summ:
                        head += f" (요약: {summ[:200]})"
                    blocks.append(head + "\n" + excerpt)
                else:
                    # 본문을 못 읽으면(삭제 등) 요약·키워드로 대체
                    kws = ", ".join(h.get("keywords") or [])
                    summ = (h.get("summary") or "").strip()
                    blocks.append(
                        f"[파일: {h['filename']}] 요약: {summ}"
                        + (f" / 키워드: {kws}" if kws else "")
                    )
            context = (
                "아래는 사용자의 로컬 파일 내용입니다. 이 내용을 바탕으로 질문에 "
                "구체적으로 답하세요. 참고한 파일명을 함께 언급하세요.\n\n"
                + "\n\n".join(blocks)
                + "\n\n질문: "
            )
            full = context + prompt
        refs = []
        for h in hits or []:
            fn = h.get("filename", "")
            if not fn:
                continue
            refs.append({"filename": fn, "path": h.get("path", ""),
                         "score": h.get("score"),
                         "chunk_index": h.get("chunk_index"),
                         "snippet": (h.get("snippet") or "")[:200],
                         "mode": "chunk"})
        return full, refs

    @staticmethod
    def _with_refs(answer, refs):
        """모델이 참고 파일을 [파일명] 형식으로 빠뜨렸으면 끝에 자동으로 붙인다."""
        answer = answer or ""
        if not refs:
            return answer
        missing = [f for f in refs if f not in answer]
        if not missing:
            return answer
        tail = " ".join("[" + f + "]" for f in missing)
        sep = "\n\n" if answer.strip() else ""
        return answer.rstrip() + sep + "참고 파일: " + tail

    def _history_block(self):
        """최근 대화를 프롬프트 앞에 붙여 후속 질문('걔는 왜?')을 잇는다.
        고정 프리픽스(CHAT_SYSTEM) 바로 뒤라 llama-server 프롬프트 캐싱과도
        어긋나지 않는다(히스토리가 바뀌는 지점 이후만 재계산)."""
        if not self.chat_history:
            return ""
        lines = ["이전 대화(맥락 참고용):"]
        for q, a in self.chat_history[-3:]:
            lines.append("사용자: " + q[:300])
            lines.append("비서: " + a[:400])
        return "\n".join(lines) + "\n\n"

    def _remember(self, prompt, answer):
        self.chat_history.append((prompt, answer))
        del self.chat_history[:-6]

    def _retrieval_query(self, prompt):
        """짧은 후속 질문은 직전 질문을 붙여 검색 재현율을 높인다."""
        p = (prompt or "").strip()
        if len(p) < 12 and self.chat_history:
            return self.chat_history[-1][0] + " " + p
        return p

    def chat(self, prompt):
        self.llama.ensure_model("text")
        full, refs = self._augment(prompt)
        # Gemma 계열은 system 역할을 지원하지 않아 지시문을 프롬프트 앞에 붙인다.
        full = CHAT_SYSTEM + "\n\n" + self._history_block() + full
        names = [r["filename"] for r in refs]
        answer = self._with_refs(self.llama.chat(full), names)
        self._remember(prompt, answer)
        return answer, refs

    def chat_stream(self, prompt, on_delta):
        """스트리밍 대화. 조각을 on_delta로 흘리며 (답변, 근거) 반환."""
        self.llama.ensure_model("text")
        full, refs = self._augment(prompt)
        full = CHAT_SYSTEM + "\n\n" + self._history_block() + full
        answer = self.llama.chat_stream(full, on_delta)
        names = [r["filename"] for r in refs]
        answer = self._with_refs(answer, names)
        self._remember(prompt, answer)
        return answer, refs

    # ================= chat 우선 처리 ==================================
    def service_chats(self):
        """대기 중인 chat 요청을 모두 처리한다(먼저 온 순서대로).

        호출 지점: 파이프라인 checkpoint(파일 사이), 키워드 조각 사이,
        중량 스레드 유휴 루프. 캐싱보다 대화 응답이 항상 우선한다."""
        while True:
            try:
                msg = self.chat_q.get_nowait()
            except queue.Empty:
                return
            rid = msg.get("id")
            try:
                t0 = time.time()
                first = [0.0]  # 첫 토큰 시각 (TTFT 측정)

                def on_delta(d, _rid=rid):
                    if not first[0]:
                        first[0] = time.time()
                    if _rid is not None:
                        _emit({"type": "chat-chunk", "id": _rid, "delta": d})
                answer, refs = self.chat_stream(msg.get("prompt", ""), on_delta)
                t1 = time.time()
                ft = first[0] or t1
                perf = {
                    "ttftMs": int((ft - t0) * 1000),          # 질문→첫 토큰
                    "genMs": int((t1 - ft) * 1000),           # 생성 구간
                    "chars": len(answer or ""),               # 생성 속도 = chars/genMs
                }
                _emit({"type": "result", "id": rid, "ok": True,
                       "answer": answer, "refs": refs, "perf": perf})
            except Exception as e:
                _log("chat 처리 실패: " + str(e))
                _emit({"type": "result", "id": rid, "ok": False, "error": str(e)})

    def checkpoint(self):
        """Pipeline ctx 훅(선택): 파일 처리 사이마다 호출돼
        캐싱을 잠시 멈추고 대기 중인 대화를 먼저 처리한다."""
        self._pause_wait()  # 일시정지면 여기(파일 경계)서 멈춘다
        self.service_chats()

    def should_stop(self):
        """Pipeline ctx 훅(선택): True면 배치를 파일 경계에서 중단한다."""
        return self.cancel_ev.is_set()


# 모델 실행이 필요해 오래 걸리는 명령 — 전용 스레드에서 순차 처리한다.
# 나머지(list/gather/clear/uncache/prune)는 stdin 스레드가 즉시 응답해,
# scan이 몇 분씩 돌아도 UI 목록 갱신이 굶지 않는다.
# chat 은 여기 속하지 않는다 — 우선 큐(agent.chat_q)로 들어가
# 파이프라인의 파일/조각 경계에서 캐싱보다 먼저 처리된다.
HEAVY_TYPES = ("cache", "cache-batch", "scan")


def _handle_heavy(agent, msg):
    """중량 명령 1건 처리(중량 스레드 전용 — agent.conn 은 이 스레드만 쓴다)."""
    t = msg.get("type")
    rid = msg.get("id")
    # 이전 취소(DB 초기화)의 잔여 플래그 해제 — 새 명령은 처음부터 실행한다.
    agent.cancel_ev.clear()
    try:
        if t == "cache":
            r = agent.cache_file(msg.get("path", ""), force=bool(msg.get("force")))
            _emit({"type": "result", "id": rid, **r})
        elif t == "cache-batch":
            r = agent.cache_batch(msg.get("paths", []), force=bool(msg.get("force")))
            if rid is not None:
                _emit({"type": "result", "id": rid, "ok": True, **r})
        elif t == "scan":
            # 전 폴더를 하나의 배치로 — 모든 파일 1→2단계 완료 후에만 3단계
            r = agent.scan_folders(msg.get("paths", []))
            _emit({"type": "result", "id": rid, "ok": True, "scan": r})
    except Exception as e:
        _log(f"명령 처리 실패({t}): {e}")
        _emit({"type": "result", "id": rid, "ok": False, "error": str(e)})


def _whisper_warmup(agent, *, reload_cfg=False):
    """whisper 서버 상시 기동(백그라운드). 설치 직후를 위해 설정 리로드 지원.
    임베딩 서버처럼 '세션만 살려두는' 역할 — 사용 방식은 기존과 동일하다."""
    def _go():
        try:
            if reload_cfg and getattr(agent, "config_path", None):
                fresh = load_config(agent.config_path)
                for k in ("whisperServerExe", "whisperModel", "whisperPort",
                          "whisperLanguage", "ffmpegExe"):
                    if fresh.get(k) not in (None, ""):
                        agent.cfg[k] = fresh[k]
            if agent.whisper.configured():
                agent.whisper.ensure()
                _log("whisper-server 상시 기동 완료")
        except Exception as e:
            _log("whisper 상시 기동 실패(요청 시 재시도): " + str(e))
    threading.Thread(target=_go, daemon=True, name="whisper-warmup").start()


def run_daemon(agent, db_path):
    # 지원 확장자를 함께 알려 Electron 쪽 필터가 이 목록을 그대로 쓰게 한다
    # (JS·Python 이중 정의로 인한 드리프트 방지 — 소스는 router 하나)
    _emit({"type": "ready",
           "exts": sorted(router.TEXT_EXTS | router.IMAGE_EXTS
                          | router.PDF_EXTS | router.AUDIO_EXTS)})
    # 회의용 whisper 서버는 상시 기동(설정돼 있을 때) — 실시간 전사 즉시 시작
    _whisper_warmup(agent)
    # 경량 명령 전용 연결 — 중량 스레드(agent.conn)와 분리된 별도 연결이라
    # WAL 모드에서 서로 차단 없이 동시에 읽고 쓸 수 있다.
    lconn = dbmod.connect(db_path)
    heavy_q = queue.Queue()

    def heavy_loop():
        while True:
            # 유휴 상태에서도 chat이 캐싱 명령보다 항상 먼저 처리되게
            # 짧은 대기로 폴링한다(캐싱 중에는 파이프라인 checkpoint가 담당).
            agent.service_chats()
            try:
                m = heavy_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if m is None:
                return
            _handle_heavy(agent, m)

    th = threading.Thread(target=heavy_loop, daemon=True, name="cache-heavy")
    th.start()
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = msg.get("type")
            rid = msg.get("id")
            if t == "shutdown":
                _emit({"type": "result", "id": rid, "ok": True})
                break
            if t == "chat":
                # 우선 큐: 캐싱(스캔) 중이면 파일/조각 경계에서,
                # 유휴면 중량 스레드 폴링에서 즉시 처리된다.
                agent.chat_q.put(msg)
                continue
            if t in HEAVY_TYPES:
                heavy_q.put(msg)
                continue
            # 어떤 명령이 실패해도 데몬은 계속 살아서 다음 명령을 처리한다.
            # (예외가 새어 나가면 워커 전체가 죽어 목록·캐싱이 모두 멈춘다)
            try:
                if t == "uncache":
                    r = agent.uncache(msg.get("path", ""), prefix=bool(msg.get("prefix")),
                                      conn=lconn)
                    if rid is not None:
                        _emit({"type": "result", "id": rid, **r})
                elif t == "prune":
                    removed = agent.prune(conn=lconn)
                    if rid is not None:
                        _emit({"type": "result", "id": rid, "ok": True, "removed": removed})
                elif t == "live-start":
                    # 스트리밍 라이브 자막(faster-whisper) 세션 시작.
                    # 모델 로드/다운로드가 길 수 있어 스레드로 처리한다.
                    def _lgo(_rid=rid):
                        try:
                            if getattr(agent, "live", None):
                                _emit({"type": "result", "id": _rid, "ok": True,
                                       "mode": "stream"})
                                return
                            from .live_stt import LiveSTT
                            agent.live = LiveSTT(agent.cfg, _emit, _log)
                            _emit({"type": "result", "id": _rid, "ok": True,
                                   "mode": "stream", "device": agent.live.device})
                        except ImportError:
                            _emit({"type": "result", "id": _rid, "ok": False,
                                   "error": "faster-whisper 미설치 (pip install faster-whisper)"})
                        except Exception as le:
                            _emit({"type": "result", "id": _rid, "ok": False,
                                   "error": str(le)})
                    threading.Thread(target=_lgo, daemon=True,
                                     name="live-start").start()
                elif t == "live-audio":
                    live = getattr(agent, "live", None)
                    if live:
                        try:
                            import base64
                            live.feed(base64.b64decode(msg.get("b64") or ""))
                        except Exception as le:
                            _log("live-audio 오류: " + str(le))
                elif t == "live-stop":
                    live = getattr(agent, "live", None)
                    agent.live = None
                    if live:
                        # 꼬리 확정(final emit)까지 스레드로 — stdin을 막지 않는다
                        threading.Thread(target=live.stop, daemon=True,
                                         name="live-stop").start()
                    if rid is not None:
                        _emit({"type": "result", "id": rid, "ok": True})
                elif t == "whisper-ensure":
                    # 선기동 보증 — 상시 기동이 실패했거나 설치 직후(설정 변경)를
                    # 위해 설정을 리로드한 뒤 기동한다. 모델 로드가 길어 스레드로.
                    def _wgo(_rid=rid):
                        try:
                            if getattr(agent, "config_path", None):
                                fresh = load_config(agent.config_path)
                                for k in ("whisperServerExe", "whisperModel",
                                          "whisperPort", "whisperLanguage",
                                          "ffmpegExe"):
                                    if fresh.get(k) not in (None, ""):
                                        agent.cfg[k] = fresh[k]
                            agent.whisper.ensure()
                            _emit({"type": "result", "id": _rid, "ok": True})
                        except Exception as we:
                            _emit({"type": "result", "id": _rid,
                                   "ok": False, "error": str(we)})
                    threading.Thread(target=_wgo, daemon=True,
                                     name="whisper-ensure").start()
                elif t == "register":
                    # 파일을 목록(대기)에 즉시 등록만 한다 — 분석은 별도 cache 명령.
                    # 녹음 직후 heavy 큐가 바빠도 UI에 바로 보이게 하는 용도.
                    paths = [p for p in (msg.get("paths") or [])
                             if p and os.path.isfile(p)]
                    if paths:
                        agent._register_ordered(
                            msg.get("label") or "(수동)", paths, conn=lconn)
                    if rid is not None:
                        _emit({"type": "result", "id": rid, "ok": True,
                               "count": len(paths)})
                elif t == "extract-text":
                    # 뷰어의 '근거 하이라이트'용 본문 추출(PDF 텍스트 레이어 등).
                    # 스캔 PDF는 이미지 렌더링 없이 저장된 OCR 본문을 쓴다.
                    p = msg.get("path", "")
                    body = ""
                    try:
                        ext = os.path.splitext(p)[1].lower()
                        if ext == ".pdf":
                            body = (extract._read_pdf_text(p) or "").strip()
                        else:
                            prep = extract.prepare(p, ext)
                            body = (prep.get("text") or "").strip()
                    except Exception:
                        body = ""
                    if not body:
                        try:
                            body = (dbmod.get_ocr_text(lconn, p) or "").strip()
                        except Exception:
                            body = ""
                    _emit({"type": "result", "id": rid, "ok": True,
                           "text": body[:500_000]})
                elif t == "chat-reset":
                    agent.chat_history = []  # 대화 맥락 초기화(기록 삭제와 연동)
                    if rid is not None:
                        _emit({"type": "result", "id": rid, "ok": True})
                elif t == "pause":
                    agent.pause_ev.set()
                    _emit({"type": "cache-event", "phase": "paused"})
                    if rid is not None:
                        _emit({"type": "result", "id": rid, "ok": True})
                elif t == "resume":
                    agent.pause_ev.clear()
                    _emit({"type": "cache-event", "phase": "resumed"})
                    if rid is not None:
                        _emit({"type": "result", "id": rid, "ok": True})
                elif t == "engine-status":
                    st = agent.llama.status()
                    st["paused"] = agent.pause_ev.is_set()
                    st["whisper"] = agent.whisper.status()
                    _emit({"type": "result", "id": rid, "ok": True, "engine": st})
                elif t == "engine-restart":
                    target = str(msg.get("target") or "all")
                    agent.llama.restart(target)
                    if target in ("whisper", "all"):
                        agent.whisper.stop()
                        _whisper_warmup(agent, reload_cfg=True)  # 상시 유지 — 즉시 재기동
                    _emit({"type": "result", "id": rid, "ok": True})
                elif t == "searchtest":
                    hits = agent._retrieve(
                        msg.get("query", ""),
                        limit=int(msg.get("limit", 8) or 8), conn=lconn)
                    _emit({"type": "result", "id": rid, "ok": True, "hits": hits})
                elif t == "clear":
                    # 1) 진행 중인 배치를 파일/조각 경계에서 중단시키고
                    agent.cancel_ev.set()
                    # 2) 아직 시작 안 한 중량 명령(구 스캔 등)을 폐기한 뒤
                    while True:
                        try:
                            stale = heavy_q.get_nowait()
                        except queue.Empty:
                            break
                        if stale is None:  # 종료 신호는 되돌려 놓는다
                            heavy_q.put(None)
                            break
                        srid = stale.get("id")
                        if srid is not None:
                            _emit({"type": "result", "id": srid, "ok": False,
                                   "error": "취소됨(DB 초기화)"})
                    # 3) DB를 비운다
                    agent.clear_all(conn=lconn)
                    if rid is not None:
                        _emit({"type": "result", "id": rid, "ok": True})
                elif t == "gather":
                    count = agent.gather_folders(msg.get("paths", []), conn=lconn)
                    _emit({"type": "result", "id": rid, "ok": True, "count": count})
                elif t == "list":
                    entries = dbmod.list_entries(lconn)
                    # 파일별 임베딩 청크 수(라이브러리 상세·검색 가능 여부 표시용)
                    try:
                        counts = dict(lconn.execute(
                            "SELECT path, COUNT(*) FROM chunk_vectors GROUP BY path"
                        ).fetchall())
                    except Exception:
                        counts = {}
                    for e in entries:
                        e["chunks"] = counts.get(e["path"], 0)
                    _emit({"type": "result", "id": rid, "ok": True,
                           "entries": entries})
                else:
                    _emit({"type": "result", "id": rid, "ok": False, "error": f"unknown type {t}"})
            except Exception as e:
                _log(f"명령 처리 실패({t}): {e}")
                _emit({"type": "result", "id": rid, "ok": False, "error": str(e)})
    finally:
        heavy_q.put(None)
        agent.close()


def _force_utf8_io():
    # PyInstaller로 얼린 exe 등에서 Windows 기본 인코딩(cp949)으로 한글이 깨지는 것을 막는다.
    # 환경변수(PYTHONUTF8)에 의존하지 않고 stdin/stdout/stderr를 UTF-8로 강제한다.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    _force_utf8_io()
    _install_kill_on_close_job()  # 워커가 죽으면 llama-server도 OS가 함께 종료
    ap = argparse.ArgumentParser(description="LevACache 캐싱 워커")
    ap.add_argument("--config")
    ap.add_argument("--db")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--cache", nargs="*", default=[])
    ap.add_argument("--scan", nargs="*", default=[])
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    db_path = args.db or cfg.get("dbPath") or os.path.join(os.getcwd(), "leva-cache.db")
    conn = dbmod.connect(db_path)
    agent = CacheAgent(cfg, conn)
    agent.config_path = args.config  # whisper 설정 리로드용

    try:
        if args.daemon:
            run_daemon(agent, db_path)
        elif args.cache:
            for p in args.cache:
                print(json.dumps(agent.cache_file(p), ensure_ascii=False))
        elif args.scan:
            for folder in args.scan:
                print(json.dumps({folder: agent.scan_folder(folder)}, ensure_ascii=False))
        else:
            ap.print_help()
    finally:
        if not args.daemon:
            agent.close()


if __name__ == "__main__":
    main()
