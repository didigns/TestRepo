"""3단계 캐싱 파이프라인 (싼 것 → 비싼 것). 실제 실행 로직의 단일 소스.

  1단계 prepare  : 본문 준비. 텍스트 파일은 본문 추출,
                   이미지·스캔PDF는 비전 모델(MiniCPM-V) OCR.
                   OCR 결과는 DB에 영속돼 재시도·채팅에서 재사용된다.
  2단계 embed    : 본문을 청크로 나눠 임베딩(별도 임베딩 서버, 메인 모델과 무관).
                   이 단계가 끝나면 의미검색·채팅이 가능하다.
  3단계 keywords : Gemma로 키워드·요약을 map-refine 반복 추출(가장 비쌈).

진행 상태(db.file_cache): status PENDING → CACHED / FAILED
진행 단계(stage 컬럼)  : 0 대기 · 1 본문준비 · 2 임베딩 · 3 키워드

배치(process_many)는 '단계 우선(breadth-first)'으로 돈다:
전 파일 1단계 → 전 파일 2단계 → 전 파일 3단계.
OCR(vision)은 1단계에, Gemma(text)는 3단계에 몰리므로
메인 모델 vision↔text 스왑이 배치당 최대 1회로 줄어든다.
단일 파일(process_one)은 같은 단계를 깊이 우선으로 수행한다.

이 모듈은 db/llama 를 직접 임포트하지 않고 ctx(의존성)를 주입받아,
어떤 저장소·모델과도 결합 없이 단독 테스트할 수 있다.
ctx 가 제공해야 하는 것:
    # 파일/라우팅
    isfile(path) -> bool
    basename(path) -> str
    size(path) -> int
    route(path) -> (model_key, ext)          # 'vision' | 'text' | None
    file_hash(path) -> str
    prepare(path, ext) -> dict               # {kind:'text'|'images', text, images, note}
    # 모델
    ensure_vision(); ensure_text()           # 메인 서버 스왑
    ocr(images, name) -> str                 # 비전 OCR
    embed_store(path, text, fallback) -> int # 청크 임베딩 저장, 저장 청크 수
    run_keywords(name, text, path=None) -> (kw, summary)
    # 저장소
    needs_caching(path, content_hash) -> bool
    upsert_pending(path, ext, model_key, content_hash)
    get_ocr_text(path) -> str | None         # 해시 동일 시에만 값 존재
    set_ocr_text(path, text)
    set_stage(path, stage)
    set_cached(path, keywords, summary)
    set_failed(path, error)
    remove(path)
    # 알림(옵션)
    emit(event: dict); log(msg)
    # 체크포인트(옵션) — 파일 처리 사이마다 호출된다.
    # 캐싱을 잠시 멈추고 우선순위 작업(예: 대화 응답)을 처리하는 용도.
    checkpoint()
    # 중단(옵션) — True면 배치를 파일 경계에서 즉시 끝낸다(DB 초기화 등).
    should_stop() -> bool
"""

# stage 값(파일 진행 단계)
STAGE_WAIT = 0
STAGE_PREPARE = 1
STAGE_EMBED = 2
STAGE_KEYWORDS = 3

# prepare_one 반환 상태
READY = "ready"          # 본문 준비 완료 → 다음 단계 진행
SKIPPED = "skipped"      # 미지원 형식
UNCHANGED = "unchanged"  # 해시 동일, 이미 캐싱됨
EMPTY = "empty"          # 0바이트 빈 파일(엔트리 제거)
MISSING = "missing"      # 파일 없음


class Pipeline:
    def __init__(self, ctx):
        self.ctx = ctx

    # ---- 공통 ----------------------------------------------------------
    def _emit(self, **ev):
        emit = getattr(self.ctx, "emit", None)
        if emit:
            try:
                emit(ev)
            except Exception:
                pass

    def _log(self, msg):
        log = getattr(self.ctx, "log", None)
        if log:
            try:
                log(msg)
            except Exception:
                pass

    def _checkpoint(self):
        """파일 처리 사이의 양보 지점. ctx가 checkpoint를 제공하면
        우선순위 작업(대화 등)을 먼저 처리하게 한다. 실패해도 캐싱은 계속."""
        cp = getattr(self.ctx, "checkpoint", None)
        if cp:
            try:
                cp()
            except Exception:
                pass

    def _should_stop(self):
        """중단 요청(DB 초기화 등) 여부. ctx가 should_stop을 제공할 때만."""
        ss = getattr(self.ctx, "should_stop", None)
        if ss:
            try:
                return bool(ss())
            except Exception:
                return False
        return False

    # ---- 1단계: 본문 준비 (텍스트 추출 / 비전 OCR) ----------------------
    def prepare_one(self, path, *, force=False):
        """한 파일의 본문을 준비한다. 반환: (state, body).

        state 가 READY 가 아니면 body 는 빈 문자열이며 이후 단계를 건너뛴다.
        예외는 호출자가 잡아 set_failed 처리한다.
        """
        c = self.ctx
        if not c.isfile(path):
            return MISSING, ""
        model_key, ext = c.route(path)
        if model_key is None:
            return SKIPPED, ""
        content_hash = c.file_hash(path)
        if not force and not c.needs_caching(path, content_hash):
            return UNCHANGED, ""

        fname = c.basename(path)
        c.upsert_pending(path, ext=ext, model_key=model_key, content_hash=content_hash)
        c.set_stage(path, STAGE_PREPARE)
        self._emit(phase="start", path=path, filename=fname,
                   model=model_key, stage=STAGE_PREPARE)

        prep = c.prepare(path, ext)
        if prep["kind"] == "text" and not prep["text"] and not prep["images"]:
            if c.size(path) == 0:
                # 실제 0바이트 빈 파일 → 조용히 건너뜀
                c.remove(path)
                self._emit(phase="skip", path=path, filename=fname, reason="빈 파일")
                return EMPTY, ""
            # 내용은 있으나 추출 실패(라이브러리 미설치 등)
            raise RuntimeError(
                "본문 추출 실패: " + (prep.get("note") or "형식 미지원/라이브러리 없음")
            )

        if prep["kind"] == "images":
            # 해시가 같은 재시도(키워드 재추출 등)면 저장된 OCR을 재사용
            body = c.get_ocr_text(path) or ""
            if not body:
                self._emit(phase="progress", path=path, filename=fname,
                           step="이미지에서 글자 추출 중 (MiniCPM)", stage=STAGE_PREPARE)
                c.ensure_vision()
                body = c.ocr(prep["images"], fname) or ""
                c.set_ocr_text(path, body)
        elif prep["kind"] == "audio":
            # 전사도 OCR과 동일하게 재사용(해시 같으면 재전사 안 함)
            body = c.get_ocr_text(path) or ""
            if not body:
                self._emit(phase="progress", path=path, filename=fname,
                           step="음성 전사 중 (Whisper)", stage=STAGE_PREPARE)
                body = c.transcribe(path) or ""
                c.set_ocr_text(path, body)
        else:
            body = prep.get("text", "") or ""
        return READY, body

    # ---- 2단계: 임베딩 (이후 의미검색·채팅 가능) ------------------------
    def embed_one(self, path, body):
        """청크 임베딩 저장. 실패해도 치명적이지 않아 3단계는 계속 진행한다."""
        c = self.ctx
        fname = c.basename(path)
        try:
            n = c.embed_store(path, body, fname)
        except Exception as e:
            self._log("임베딩 실패: " + str(e))
            return 0
        c.set_stage(path, STAGE_EMBED)
        self._emit(phase="embedded", path=path, filename=fname,
                   chunks=n, stage=STAGE_EMBED)
        return n

    # ---- 3단계: 키워드/요약 (Gemma) -------------------------------------
    def keywords_one(self, path, body, *, qindex=None, qtotal=None):
        c = self.ctx
        fname = c.basename(path)
        c.ensure_text()
        c.set_stage(path, STAGE_KEYWORDS)
        ev = {"phase": "progress", "path": path, "filename": fname,
              "step": "내용 분석 중 (Gemma)", "stage": STAGE_KEYWORDS}
        if qtotal:
            ev.update(qindex=qindex, qtotal=qtotal)
        self._emit(**ev)
        keywords, summary = c.run_keywords(fname, body or "", path=path)
        c.set_cached(path, keywords, summary)
        self._emit(phase="done", path=path, filename=fname,
                   keywords=keywords, summary=summary, stage=STAGE_KEYWORDS)
        return keywords, summary

    # ---- 단일 파일: 깊이 우선(1→2→3) ------------------------------------
    def process_one(self, path, *, force=False):
        c = self.ctx
        try:
            state, body = self.prepare_one(path, force=force)
        except Exception as e:
            c.set_failed(path, e)
            self._emit(phase="error", path=path,
                       filename=c.basename(path), error=str(e))
            return {"ok": False, "path": path, "error": str(e)}

        if state == MISSING:
            return {"ok": False, "path": path, "error": "파일 없음"}
        if state == SKIPPED:
            _key, ext = c.route(path)
            return {"ok": False, "path": path, "skipped": True,
                    "error": f"지원하지 않는 형식({ext})"}
        if state == UNCHANGED:
            return {"ok": True, "path": path, "cached": True, "unchanged": True}
        if state == EMPTY:
            return {"ok": True, "path": path, "skipped": True, "empty": True}

        nchunks = self.embed_one(path, body)
        try:
            keywords, summary = self.keywords_one(path, body)
        except Exception as e:
            c.set_failed(path, e)
            self._emit(phase="error", path=path,
                       filename=c.basename(path), error=str(e))
            return {"ok": False, "path": path, "error": str(e)}
        return {"ok": True, "path": path, "keywords": keywords,
                "summary": summary, "chunks": nchunks}

    # ---- 여러 파일: 단계 우선(breadth-first) -----------------------------
    def process_many(self, paths, results=None, *, force=False):
        """여러 파일을 단계 우선으로 처리한다.

        1단계: 전 파일 본문 준비(OCR은 vision 1회 로드로 몰림)
        2단계: 전 파일 임베딩(별도 임베딩 서버 — 스왑 없음)
        3단계: 전 파일 키워드/요약(text 1회 로드) — 큐 위치를 함께 알림
        단계 사이 본문은 메모리(bodies)로 전달한다.
        """
        c = self.ctx
        if results is None:
            results = {"cached": 0, "unchanged": 0, "failed": 0, "skipped": 0}
        bodies = {}  # path -> 본문 (1→3단계 전달)

        # ---- 1단계 ----
        for path in paths:
            if self._should_stop():  # 취소(DB 초기화) → 배치 즉시 종료
                return results
            self._checkpoint()  # 파일 사이: 대기 중인 대화 먼저 처리
            try:
                state, body = self.prepare_one(path, force=force)
            except Exception as e:
                c.set_failed(path, e)
                self._emit(phase="error", path=path,
                           filename=c.basename(path), error=str(e))
                results["failed"] += 1
                continue
            if state == READY:
                bodies[path] = body
            elif state == UNCHANGED:
                results["unchanged"] += 1
            elif state in (SKIPPED, EMPTY):
                results["skipped"] += 1
            # MISSING 은 집계 없이 무시(스캔 중 삭제된 파일 등)

        # ---- 2단계 ----
        for path, body in bodies.items():
            if self._should_stop():
                return results
            self._checkpoint()
            self.embed_one(path, body)

        # ---- 3단계 ----
        if bodies:
            try:
                c.ensure_text()
            except Exception as e:
                # 텍스트 모델 기동 실패(경로 오류·포트 점유 등)는 배치 실패로
                # 격리한다 — 예외를 밖으로 던지면 데몬 전체가 죽는다.
                self._log("텍스트 모델 기동 실패: " + str(e))
                for path in bodies:
                    c.set_failed(path, e)
                    self._emit(phase="error", path=path,
                               filename=c.basename(path), error=str(e))
                    results["failed"] += 1
                return results
        qtotal = len(bodies)
        for qi, (path, body) in enumerate(bodies.items(), start=1):
            if self._should_stop():
                return results
            self._checkpoint()  # 파일 사이(조각 사이는 run_keywords가 처리)
            try:
                self.keywords_one(path, body, qindex=qi, qtotal=qtotal)
                results["cached"] += 1
            except Exception as e:
                c.set_failed(path, e)
                self._emit(phase="error", path=path,
                           filename=c.basename(path), error=str(e))
                results["failed"] += 1
        return results
