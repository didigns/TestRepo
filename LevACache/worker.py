"""LevACache 워커.

모드:
  --cache <파일...>   지정 파일 캐싱 후 종료
  --scan <폴더...>    폴더 내 지원 파일 중 해시가 바뀐 것만 캐싱
  --daemon            stdin NDJSON 명령 수신(부모=Electron), 큐잉 처리
                      명령: {"id","type":"cache","path"} / {"type":"uncache","path","prefix"} /
                            {"type":"prune"} / {"type":"clear"} / {"type":"scan","paths":[...]} /
                            {"type":"chat","prompt"} / {"type":"list"} / {"type":"shutdown"}
                      출력: {"type":"ready"} / {"type":"progress"...} /
                            {"type":"result","id",...} / {"type":"cache-event"...}

옵션: --config <leva-config.json> --db <경로>
"""
import argparse
import json
import os
import sys

from . import db as dbmod
from . import extract, router, summarize
from .config import load_config
from .llama_manager import LlamaManager


def _emit(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
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
    def __init__(self, cfg, conn):
        self.cfg = cfg
        self.conn = conn
        self.llama = LlamaManager(cfg, logger=_log)

    def close(self):
        self.llama.stop()

    def cache_file(self, path, *, force=False):
        """한 파일을 캐싱. 반환: dict(status, ...)."""
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            return {"ok": False, "path": path, "error": "파일 없음"}

        model_key, ext = router.route(path)
        if model_key is None:
            return {"ok": False, "path": path, "error": f"지원하지 않는 형식({ext})", "skipped": True}

        content_hash = extract.file_hash(path)
        if not force and not dbmod.needs_caching(self.conn, path, content_hash):
            return {"ok": True, "path": path, "cached": True, "unchanged": True}

        st = os.stat(path)
        model_name = os.path.basename(
            self.cfg.get("visionModel" if model_key == "vision" else "textModel") or ""
        )
        dbmod.upsert_pending(
            self.conn, path=path, filename=os.path.basename(path), ext=ext,
            size_bytes=st.st_size, mtime=str(int(st.st_mtime)),
            content_hash=content_hash, model_key=model_key, model_name=model_name,
        )
        _emit({"type": "cache-event", "phase": "start", "path": path,
               "filename": os.path.basename(path), "model": model_key})

        try:
            prep = extract.prepare(path, ext, pdf_max_pages=self.cfg.get("pdfMaxPages", 5))
            if prep["kind"] == "text" and not prep["text"] and not prep["images"]:
                if st.st_size == 0:
                    # 실제 0바이트 빈 파일 → 조용히 건너뜀
                    dbmod.remove(self.conn, path)
                    _emit({"type": "cache-event", "phase": "skip", "path": path,
                           "filename": os.path.basename(path), "reason": "빈 파일"})
                    return {"ok": True, "path": path, "skipped": True, "empty": True}
                # 내용은 있으나 추출 실패(라이브러리 미설치 등) → 사유와 함께 실패로 기록
                raise RuntimeError(
                    "본문 추출 실패: " + (prep.get("note") or "형식 미지원/라이브러리 없음")
                )

            fname = os.path.basename(path)
            if prep["kind"] == "images":
                # 1) MiniCPM(vision)으로 이미지의 글자·내용을 텍스트로 추출(OCR)
                _emit({"type": "cache-event", "phase": "progress", "path": path,
                       "filename": fname, "step": "이미지에서 글자 추출 중 (MiniCPM)"})
                self.llama.ensure_model("vision")
                extracted = self.llama.chat(
                    summarize.build_ocr_prompt(fname), images=prep["images"]
                )
                # 2) 추출된 텍스트를 Gemma(text)로 분석
                _emit({"type": "cache-event", "phase": "progress", "path": path,
                       "filename": fname, "step": "내용 분석 중 (Gemma)"})
                self.llama.ensure_model("text")
                raw = self.llama.chat(summarize.build_prompt(fname, "text", extracted))
                used_model = "vision+text"
            else:
                # 텍스트(문서·PDF 텍스트)는 Gemma가 바로 분석
                _emit({"type": "cache-event", "phase": "progress", "path": path,
                       "filename": fname, "step": "내용 분석 중 (Gemma)"})
                self.llama.ensure_model("text")
                raw = self.llama.chat(
                    summarize.build_prompt(fname, "text", prep.get("text", ""))
                )
                used_model = "text"

            keywords, summary = summarize.parse_result(raw)
            dbmod.set_cached(self.conn, path, keywords, summary)
            _emit({"type": "cache-event", "phase": "done", "path": path,
                   "filename": fname, "keywords": keywords,
                   "summary": summary, "model": used_model})
            return {"ok": True, "path": path, "keywords": keywords, "summary": summary}
        except Exception as e:
            dbmod.set_failed(self.conn, path, e)
            _emit({"type": "cache-event", "phase": "error", "path": path,
                   "filename": os.path.basename(path), "error": str(e)})
            return {"ok": False, "path": path, "error": str(e)}

    def _augment(self, prompt):
        """관련 캐시 파일의 본문을 재추출해 프롬프트에 컨텍스트로 덧붙인다."""
        # 상위 2개만 주입(프롬프트를 짧게 유지해 프리필/TTFT 절감)
        hits = dbmod.search(self.conn, prompt, limit=2)
        full = prompt
        if hits:
            budget = 7000  # 컨텍스트에 넣을 총 본문 문자 예산
            blocks = []
            for h in hits:
                path = h.get("path", "")
                body = ""
                try:
                    if os.path.isfile(path):
                        _key, ext = router.route(path)
                        prep = extract.prepare(
                            path, ext, pdf_max_pages=self.cfg.get("pdfMaxPages", 5)
                        )
                        body = (prep.get("text") or "").strip()
                except Exception:
                    body = ""
                if body and budget > 0:
                    excerpt = body[:budget]
                    budget -= len(excerpt)
                    blocks.append(f"[파일: {h['filename']}]\n{excerpt}")
                else:
                    # 본문을 못 읽으면(이미지/삭제 등) 요약·키워드로 대체
                    kws = ", ".join(h.get("keywords") or [])
                    summ = (h.get("summary") or "").strip()
                    blocks.append(
                        f"[파일: {h['filename']}] 요약: {summ}"
                        + (f" / 키워드: {kws}" if kws else "")
                    )
                if budget <= 0:
                    break
            context = (
                "아래는 사용자의 로컬 파일 내용입니다. 이 내용을 바탕으로 질문에 "
                "구체적으로 답하세요. 참고한 파일명을 함께 언급하세요.\n\n"
                + "\n\n".join(blocks)
                + "\n\n질문: "
            )
            full = context + prompt
        refs = [h.get("filename", "") for h in hits] if hits else []
        return full, [r for r in refs if r]

    def _with_refs(self, answer, refs):
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

    def chat(self, prompt):
        self.llama.ensure_model("text")
        full, refs = self._augment(prompt)
        # Gemma 계열은 system 역할을 지원하지 않아 지시문을 프롬프트 앞에 붙인다.
        full = CHAT_SYSTEM + "\n\n" + full
        return self._with_refs(self.llama.chat(full), refs)

    def chat_stream(self, prompt, on_delta):
        """스트리밍 대화. 조각을 on_delta로 흘리며 전체 문자열 반환."""
        self.llama.ensure_model("text")
        full, refs = self._augment(prompt)
        full = CHAT_SYSTEM + "\n\n" + full
        answer = self.llama.chat_stream(full, on_delta)
        return self._with_refs(answer, refs)

    def uncache(self, path, *, prefix=False):
        """삭제/이동된 파일(또는 폴더)의 캐시 엔트리를 제거."""
        path = os.path.abspath(path)
        if prefix:
            removed = dbmod.remove_under(self.conn, path)
        else:
            dbmod.remove(self.conn, path)
            removed = 1
        return {"ok": True, "path": path, "removed": removed}

    def prune(self):
        """디스크에 더 이상 존재하지 않는 파일의 캐시 엔트리를 모두 제거."""
        removed = 0
        paths = [row[0] for row in
                 self.conn.execute("SELECT path FROM file_cache").fetchall()]
        for p in paths:
            if not os.path.isfile(p):
                dbmod.remove(self.conn, p)
                removed += 1
        return removed

    def scan_folder(self, folder):
        results = {"discovered": 0, "cached": 0, "unchanged": 0, "failed": 0, "skipped": 0}
        # 지원 파일을 모으되, 이미지 확장자는 뒤로 정렬한다.
        # 텍스트류(문서·대개의 PDF)를 먼저 처리하면 text 모델이 한 번만 로드되고,
        # 이미지류는 마지막에 몰려 text↔vision 서버 재기동(스왑)이 최소화된다.
        text_like, image_like = [], []
        for root, _dirs, files in os.walk(folder):
            for fn in files:
                p = os.path.join(root, fn)
                if not router.is_supported(p):
                    continue
                ext = os.path.splitext(fn)[1].lower()
                (image_like if ext in router.IMAGE_EXTS else text_like).append(p)

        for p in text_like + image_like:
            results["discovered"] += 1
            r = self.cache_file(p)
            if r.get("skipped"):
                results["skipped"] += 1
            elif r.get("unchanged"):
                results["unchanged"] += 1
            elif r.get("ok"):
                results["cached"] += 1
            else:
                results["failed"] += 1
        return results


def run_daemon(agent):
    _emit({"type": "ready"})
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
            if t == "cache":
                r = agent.cache_file(msg.get("path", ""), force=bool(msg.get("force")))
                _emit({"type": "result", "id": rid, **r})
            elif t == "uncache":
                r = agent.uncache(msg.get("path", ""), prefix=bool(msg.get("prefix")))
                if rid is not None:
                    _emit({"type": "result", "id": rid, **r})
            elif t == "prune":
                removed = agent.prune()
                if rid is not None:
                    _emit({"type": "result", "id": rid, "ok": True, "removed": removed})
            elif t == "clear":
                dbmod.clear_all(agent.conn)
                if rid is not None:
                    _emit({"type": "result", "id": rid, "ok": True})
            elif t == "scan":
                summary = {}
                for folder in msg.get("paths", []):
                    summary[folder] = agent.scan_folder(folder)
                _emit({"type": "result", "id": rid, "ok": True, "scan": summary})
            elif t == "chat":
                try:
                    def on_delta(d, _rid=rid):
                        if _rid is not None:
                            _emit({"type": "chat-chunk", "id": _rid, "delta": d})
                    answer = agent.chat_stream(msg.get("prompt", ""), on_delta)
                    _emit({"type": "result", "id": rid, "ok": True, "answer": answer})
                except Exception as e:
                    _emit({"type": "result", "id": rid, "ok": False, "error": str(e)})
            elif t == "list":
                _emit({"type": "result", "id": rid, "ok": True,
                       "entries": dbmod.list_entries(agent.conn)})
            elif t == "shutdown":
                _emit({"type": "result", "id": rid, "ok": True})
                break
            else:
                _emit({"type": "result", "id": rid, "ok": False, "error": f"unknown type {t}"})
    finally:
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

    try:
        if args.daemon:
            run_daemon(agent)
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
