"""LevACache 워커.

모드:
  --cache <파일...>   지정 파일 캐싱 후 종료
  --scan <폴더...>    폴더 내 지원 파일 중 해시가 바뀐 것만 캐싱
  --daemon            stdin NDJSON 명령 수신(부모=Electron), 큐잉 처리
                      명령: {"id","type":"cache","path"} / {"type":"scan","paths":[...]} /
                            {"type":"list"} / {"type":"shutdown"}
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
            # 텍스트가 비었고 이미지도 없으면 실패 처리
            if prep["kind"] == "text" and not prep["text"] and not prep["images"]:
                raise RuntimeError("본문 추출 실패: " + prep.get("note", ""))

            use_key = "vision" if prep["kind"] == "images" else model_key
            # pdf가 텍스트로 폴백된 경우 텍스트 모델 사용
            if prep["kind"] == "text":
                use_key = "text"
            self.llama.ensure_model(use_key)

            prompt = summarize.build_prompt(os.path.basename(path), prep["kind"], prep.get("text", ""))
            raw = self.llama.chat(prompt, images=prep.get("images") or None)
            keywords, summary = summarize.parse_result(raw)
            dbmod.set_cached(self.conn, path, keywords, summary)
            _emit({"type": "cache-event", "phase": "done", "path": path,
                   "filename": os.path.basename(path), "keywords": keywords,
                   "summary": summary, "model": use_key})
            return {"ok": True, "path": path, "keywords": keywords, "summary": summary}
        except Exception as e:
            dbmod.set_failed(self.conn, path, e)
            _emit({"type": "cache-event", "phase": "error", "path": path,
                   "filename": os.path.basename(path), "error": str(e)})
            return {"ok": False, "path": path, "error": str(e)}

    def scan_folder(self, folder):
        results = {"discovered": 0, "cached": 0, "unchanged": 0, "failed": 0, "skipped": 0}
        for root, _dirs, files in os.walk(folder):
            for fn in files:
                p = os.path.join(root, fn)
                if not router.is_supported(p):
                    continue
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
            elif t == "scan":
                summary = {}
                for folder in msg.get("paths", []):
                    summary[folder] = agent.scan_folder(folder)
                _emit({"type": "result", "id": rid, "ok": True, "scan": summary})
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


def main(argv=None):
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
