"""LevA 캐싱 진단 스크립트.
사용법:
    python diag_cache.py                      (감시 폴더 자동 점검)
    python diag_cache.py "E:\\Work\\TestAISum"  (특정 폴더 점검)
DB(leva-cache.db)와 설정(leva-config.json)을 자동으로 찾아 폴더와 교차 점검합니다.
"""
import json
import os
import sqlite3
import sys

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
PDF_EXTS = {".pdf"}
TEXT_EXTS = {".txt", ".md", ".log", ".csv", ".json", ".xml", ".html", ".htm",
             ".docx", ".xlsx", ".pptx", ".hwp", ".rtf", ".py", ".js", ".ts"}
SUPPORTED = IMAGE_EXTS | PDF_EXTS | TEXT_EXTS


def find_appdata_files():
    roaming = os.environ.get("APPDATA", "")
    names = ["LevAAISummary", "levaaisummary"]
    cfg = db = None
    for n in names:
        base = os.path.join(roaming, n)
        c = os.path.join(base, "leva-config.json")
        d = os.path.join(base, "leva-cache.db")
        if os.path.isfile(c) and cfg is None:
            cfg = c
        if os.path.isfile(d) and db is None:
            db = d
    return cfg, db


def load_config(cfg_path):
    if not cfg_path:
        return {}
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("  설정 읽기 실패:", e)
        return {}


def hr(t):
    print("\n" + "=" * 60)
    print(t)
    print("=" * 60)


def main():
    cfg_path, db_path = find_appdata_files()

    hr("1) 설정·DB 위치")
    print("  config:", cfg_path or "(찾지 못함)")
    print("  db    :", db_path or "(찾지 못함)")

    cfg = load_config(cfg_path)
    watched = [w.get("path") for w in (cfg.get("watchedFolders") or []) if w.get("path")]

    hr("2) 설정 요약")
    print("  자동 캐싱(enableCache):", cfg.get("enableCache", "?"))
    print("  llama-server.exe      :", cfg.get("llamaServerExe", ""),
          "→", "존재" if os.path.isfile(cfg.get("llamaServerExe", "") or "") else "없음")
    for key in ("textModel", "visionModel", "visionMmproj"):
        p = cfg.get(key, "")
        print(f"  {key:13}:", p, "→", "존재" if os.path.isfile(p or "") else "없음")
    print("  감시 폴더:")
    if watched:
        for w in watched:
            print("    -", w, "→", "존재" if os.path.isdir(w) else "없음")
    else:
        print("    (없음) — 감시 폴더가 없으면 자동 캐싱이 안 됩니다.")

    # DB 로드
    rows = []
    if db_path:
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT path, filename, ext, model_key, status, error_msg, keywords, cached_at "
                "FROM file_cache ORDER BY cached_at DESC"
            ).fetchall()
        except Exception as e:
            print("  DB 읽기 실패:", e)

    by_status = {}
    db_paths = {}
    for r in rows:
        by_status.setdefault(r["status"], []).append(r)
        db_paths[os.path.normcase(os.path.abspath(r["path"]))] = r

    hr("3) DB 현황 (총 %d건)" % len(rows))
    for st in ("CACHED", "PENDING", "FAILED"):
        items = by_status.get(st, [])
        print(f"  {st}: {len(items)}건")
        for r in items[:50]:
            extra = ""
            if st == "FAILED" and r["error_msg"]:
                extra = "  ← " + r["error_msg"][:80]
            if st == "CACHED":
                try:
                    kw = ", ".join(json.loads(r["keywords"] or "[]")[:4])
                except Exception:
                    kw = ""
                extra = "  [" + kw + "]"
            print(f"     {r['filename']}{extra}")

    # 대상 폴더 결정
    targets = []
    if len(sys.argv) > 1:
        targets = [sys.argv[1]]
    elif watched:
        targets = watched
    else:
        targets = [r"E:\Work\TestAISum"]

    hr("4) 폴더 교차 점검")
    for folder in targets:
        print("\n  [폴더]", folder)
        if not os.path.isdir(folder):
            print("    → 폴더가 존재하지 않습니다.")
            continue
        supported, unsupported, uncached, failed_here = [], [], [], []
        for root, _dirs, files in os.walk(folder):
            for fn in files:
                p = os.path.join(root, fn)
                ext = os.path.splitext(fn)[1].lower()
                if ext not in SUPPORTED:
                    unsupported.append(fn)
                    continue
                supported.append(fn)
                key = os.path.normcase(os.path.abspath(p))
                r = db_paths.get(key)
                if r is None:
                    uncached.append(fn)
                elif r["status"] == "FAILED":
                    failed_here.append((fn, r["error_msg"] or ""))
        print(f"    지원 파일: {len(supported)}개, 미지원(스킵): {len(unsupported)}개")
        if uncached:
            print(f"    ⚠ 아직 캐싱 안 됨({len(uncached)}개):")
            for fn in uncached[:50]:
                print("       -", fn)
        if failed_here:
            print(f"    ✕ 실패({len(failed_here)}개):")
            for fn, err in failed_here[:50]:
                print("       -", fn, "←", err[:80])
        if not uncached and not failed_here and supported:
            print("    ✓ 지원 파일 모두 캐싱 완료")

    # 유령(디스크에 없는데 DB에 남은 것)
    hr("5) 유령 캐시(디스크에 없는 파일)")
    ghosts = [r for r in rows if not os.path.isfile(r["path"])]
    if ghosts:
        print(f"  {len(ghosts)}건 — prune 대상:")
        for r in ghosts[:50]:
            print("     -", r["filename"], "(", r["path"], ")")
    else:
        print("  없음")

    print("\n완료.")


if __name__ == "__main__":
    main()
