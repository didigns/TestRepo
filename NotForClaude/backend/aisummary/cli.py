"""AISummary command-line interface.

Runnable on the user's machine (where the local model backend lives):

    python -m aisummary.cli doctor                 # check backend + models
    python -m aisummary.cli pull                    # pull required models
    python -m aisummary.cli ingest  <folder>        # vectorize a folder
    python -m aisummary.cli ask     "질문..."        # grounded RAG answer
    python -m aisummary.cli eval    [golden.json]   # real quality score
    python -m aisummary.cli meeting <transcript.json>   # summary + PDF

Everything runs locally against 127.0.0.1.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .config import load_settings, save_settings, TIERS
from .hardware import recommend
from .llm import OllamaError
from .providers import make_provider
from .rag.vectorstore import VectorStore
from .rag.engine import RagEngine
from .ingest.watcher import Ingestor


def _ctx():
    s = load_settings()
    prov = make_provider(s)
    store = VectorStore(s)
    return s, prov, store


def _required_models(settings) -> list:
    if settings.backend == "llamacpp":
        return [settings.llm_gguf()[1], settings.embed_gguf()[1]]
    p = settings.profile()
    return [p.llm_model, p.embed_model]


def cmd_doctor(args):
    s, prov, store = _ctx()
    rec = recommend()
    print("=== 하드웨어 ===")
    print(rec["reason"])
    print(f"현재 티어(설정): {s.tier}")
    print(f"\n=== 로컬 모델 백엔드: {s.backend} ===")
    up = prov.is_up()
    if s.backend == "llamacpp":
        print(f"llama-cpp-python: {'설치됨' if up else '미설치 (pip install llama-cpp-python)'}")
    else:
        print(f"실행 중: {'예' if up else '아니오 (ollama serve 필요)'}")
    if not up:
        return 0
    print(f"백엔드 버전: {prov.version()}")
    installed = set(prov.installed_models())
    print(f"설치된 모델: {sorted(installed) or '없음'}")
    print("\n=== 필요 모델 점검 ===")
    ok = True
    for m in _required_models(s):
        present = any(m.split(":")[0] in im for im in installed)
        print(f"  [{'OK' if present else '없음'}] {m}")
        ok = ok and present
    print(f"\n인덱싱된 청크: {store.count()}")
    print("\n=== 생성 프로브 ===")
    try:
        out = prov.generate("한 단어로만 답하라: 하늘의 색은?", allow_fallback=False)
        print(f"  OK {s.profile().llm_model}: {out[:40]!r}")
    except OllamaError as e:
        print(f"  FAIL {e}")
        print("  -> 모델이 없으면 'python -m aisummary.cli pull' 로 GGUF 다운로드")
    print("\n결과:", "준비 완료" if ok else "-> 'python -m aisummary.cli pull' 실행 필요")
    return 0


def cmd_pull(args):
    s, prov, _ = _ctx()
    if s.backend == "llamacpp":
        prov.ensure_models(log=print)   # hf_hub_download GGUFs
        print("완료: GGUF 모델 준비됨")
    else:
        for m in _required_models(s):
            print(f"pulling {m} ...")
            subprocess.run(["ollama", "pull", m], check=False)
    return 0


def cmd_ingest(args):
    s, prov, store = _ctx()
    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"폴더 없음: {folder}", file=sys.stderr)
        return 1
    ing = Ingestor(s, store, prov, on_event=lambda ev, msg: print(f"  [{ev}] {msg}"))
    res = ing.scan_folder(folder)
    if str(folder) not in s.watch_folders:
        s.watch_folders.append(str(folder))
        save_settings(s)
    print(f"\n완료: 파일 {res['files']}개, 청크 {res['chunks']}개 인덱싱")
    return 0


def cmd_ask(args):
    s, prov, store = _ctx()
    if store.count() == 0:
        print("인덱스가 비어 있습니다. 먼저 'ingest'를 실행하세요.", file=sys.stderr)
        return 1
    eng = RagEngine(s, store, prov)
    ans = eng.query(args.question)
    print("\n" + ans.text + "\n")
    print(f"[근거 {len(ans.citations)}개 · 신뢰도 {ans.confidence:.2f} · "
          f"grounded={ans.grounded} · refused={ans.refused}]")
    for c in ans.citations:
        print(f"  [{c.label}] {c.filename} p.{c.page} (sim {c.score}) — {c.snippet[:80]}...")
    for w in ans.warnings:
        print(f"  ! {w}")
    return 0


def cmd_eval(args):
    from .rag.evaluate import evaluate, load_cases
    s, prov, store = _ctx()
    eng = RagEngine(s, store, prov)
    if args.golden and Path(args.golden).exists():
        cases = load_cases(Path(args.golden))
    else:
        default = Path(__file__).parent / "rag" / "golden.sample.json"
        cases = load_cases(default)
        print(f"(기본 골든셋 사용: {default})")
    res = evaluate(eng, cases)
    print("\n=== RAG 품질 평가 (실측) ===")
    print(f"groundedness      : {res.groundedness}")
    print(f"citation_accuracy : {res.citation_accuracy}")
    print(f"refusal_correct   : {res.refusal_correctness}")
    print(f"answer_relevance  : {res.answer_relevance}")
    print(f"RAG SCORE         : {res.rag_score} / 100  "
          f"({'PASS' if res.rag_score >= 85 else 'FAIL - 재작업 필요'})")
    return 0 if res.rag_score >= 85 else 2


def cmd_meeting(args):
    import json
    from .meetings.transcribe import Transcript, Segment
    from .meetings.minutes import summarize, render_pdf
    s, prov, store = _ctx()
    data = json.loads(Path(args.transcript).read_text(encoding="utf-8"))
    tr = Transcript(meeting_id=data["meeting_id"], title=data["title"],
                    started_at=data["started_at"],
                    segments=[Segment(**x) for x in data["segments"]])
    print("요약 생성 중 (SLM)...")
    minutes = summarize(tr, s, prov)
    pdf = render_pdf(minutes, tr)
    print(f"요약: {minutes.summary}")
    print(f"PDF 회의록: {pdf}")
    return 0


def cmd_meeting_live(args):
    import threading
    from .meetings.streaming import run_live_meeting
    from .meetings.minutes import summarize, render_pdf
    s, prov, store = _ctx()
    # language: --lang overrides; "auto" -> whisper auto-detect
    lang = getattr(args, "lang", None)
    if lang is not None:
        s.stt_language = None if lang == "auto" else lang
    print(f"(전사 언어: {s.stt_language or '자동감지'})")
    stop = threading.Event()

    def on_seg(seg):
        print(f"  [{seg.start:5.1f}s] {seg.text}")

    def waiter():
        input("")            # press Enter to end the meeting
        stop.set()
    threading.Thread(target=waiter, daemon=True).start()

    print(f"회의 '{args.title}' 녹음 시작 (마이크). 말씀하세요. "
          f"종료하려면 [Enter].\n")
    tr = run_live_meeting(s, args.title, on_seg, stop)
    print(f"\n전사 {len(tr.segments)}개 세그먼트. 요약 생성 중 (SLM)...")
    minutes = summarize(tr, s, prov)
    pdf = render_pdf(minutes, tr)
    print(f"\n요약: {minutes.summary}")
    print(f"PDF 회의록: {pdf}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="aisummary")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor").set_defaults(func=cmd_doctor)
    sub.add_parser("pull").set_defaults(func=cmd_pull)
    pi = sub.add_parser("ingest"); pi.add_argument("folder"); pi.set_defaults(func=cmd_ingest)
    pa = sub.add_parser("ask"); pa.add_argument("question"); pa.set_defaults(func=cmd_ask)
    pe = sub.add_parser("eval"); pe.add_argument("golden", nargs="?"); pe.set_defaults(func=cmd_eval)
    pm = sub.add_parser("meeting"); pm.add_argument("transcript"); pm.set_defaults(func=cmd_meeting)
    pl = sub.add_parser("meeting-live")
    pl.add_argument("--title", default="회의")
    pl.add_argument("--lang", default=None,
                    help='전사 언어: "ko","en",... 또는 "auto"(자동감지). 기본=설정값(ko)')
    pl.set_defaults(func=cmd_meeting_live)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
