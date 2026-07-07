"""모델에게 키워드+요약을 JSON으로 요청하고 파싱한다."""
import json
import re

PROMPT_TEXT = (
    "당신은 문서 색인 비서입니다. 아래 문서 내용을 분석해서 "
    "핵심 키워드와 한두 문장 요약을 JSON으로만 답하세요.\n"
    "형식: {{\"keywords\": [\"...\", \"...\"], \"summary\": \"...\"}}\n"
    "키워드는 개수 제한 없이 내용에 필요한 만큼, 한국어로. 다른 말은 하지 마세요.\n\n"
    "[문서: {name}]\n{body}"
)

PROMPT_VISION = (
    "당신은 문서/이미지 색인 비서입니다. 첨부한 이미지를 보고 "
    "핵심 키워드와 한두 문장 요약을 JSON으로만 답하세요.\n"
    "형식: {{\"keywords\": [\"...\", \"...\"], \"summary\": \"...\"}}\n"
    "키워드는 개수 제한 없이 내용에 필요한 만큼, 한국어로. 다른 말은 하지 마세요.\n\n"
    "[파일: {name}]"
)


PROMPT_OCR = (
    "당신은 이미지에서 글자를 추출하는 OCR 비서입니다. "
    "이미지에 보이는 모든 텍스트를 빠짐없이 그대로 옮겨 적고, "
    "표·도표·사진 등 텍스트가 아닌 요소는 무엇이 보이는지 간단히 설명하세요. "
    "추측하지 말고 실제로 보이는 것만 적으세요.\n\n[파일: {name}]"
)


def build_prompt(name, kind, body=""):
    if kind == "images":
        return PROMPT_VISION.format(name=name)
    return PROMPT_TEXT.format(name=name, body=body or "(본문 없음)")


def build_ocr_prompt(name):
    """비전 모델(MiniCPM)에게 이미지의 글자·내용을 텍스트로 추출하도록 요청."""
    return PROMPT_OCR.format(name=name)


def _strip_thinking(text):
    """<think>...</think> 등 사고 과정 블록 제거."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<\|?/?think\|?>", "", text)  # 잘려서 열림/닫힘만 남은 경우
    return text


def _extract_json_obj(text):
    """텍스트에서 균형 잡힌 마지막 JSON 객체를 찾아 파싱(사고 과정 뒤의 JSON 대응)."""
    starts = [i for i, c in enumerate(text) if c == "{"]
    for s in reversed(starts):
        depth = 0
        for e in range(s, len(text)):
            if text[e] == "{":
                depth += 1
            elif text[e] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[s:e + 1])
                    except Exception:
                        break
    return None


def _norm_keywords(kws):
    if isinstance(kws, str):
        kws = [k.strip() for k in re.split(r"[,\n]", kws) if k.strip()]
    return [str(k).strip() for k in (kws or []) if str(k).strip()]


def _strip_fences(text):
    """마크다운 코드펜스(```json ... ```)를 제거한다."""
    if not text:
        return text
    t = re.sub(r"```[a-zA-Z0-9_-]*", "", str(text))  # 여는 펜스 + 언어태그
    return t.replace("```", "").strip()


def _looks_like_json(s):
    """요약 자리에 JSON/펜스가 잘못 들어갔는지 판별(저사양 모델 오출력 방지)."""
    if not s:
        return False
    t = str(s).strip()
    if "```" in t:
        return True
    if t[:1] in "{[" and re.search(r'["\']?(keywords|summary)["\']?\s*:', t):
        return True
    return False


def _clean_summary(s):
    """요약 문자열 정제. JSON/펜스가 섞였으면 진짜 summary만 건지거나 버린다.
    (살릴 수 없는 JSON 덩어리를 요약으로 그대로 노출하지 않는 것이 핵심)"""
    if s is None:
        return ""
    s = _strip_fences(s).strip()
    if _looks_like_json(s):
        inner = _extract_json_obj(s)  # summary가 JSON 안에 중첩된 경우 한 번 더 파싱
        if inner is not None and isinstance(inner.get("summary"), str):
            cand = _strip_fences(inner["summary"]).strip()
            if cand and not _looks_like_json(cand):
                return cand
        return ""  # raw JSON 노출 금지 — 빈 요약이 낫다
    return s


def parse_result(raw):
    """모델 응답에서 keywords/summary 추출. 사고 과정·펜스·잘린/중첩 JSON에 견고."""
    if not raw:
        return [], ""
    text = _strip_fences(_strip_thinking(raw))

    # 1) 균형 JSON 객체 파싱
    obj = _extract_json_obj(text)
    if obj is not None:
        return _norm_keywords(obj.get("keywords")), _clean_summary(obj.get("summary"))

    # 2) JSON이 깨졌거나 잘린 경우 정규식으로 건져내기(닫는 괄호 없어도 대응)
    kws = []
    mk = re.search(r'"keywords"\s*:\s*\[([^\]]*)\]?', text, re.DOTALL)
    if mk:
        kws = _norm_keywords([p.strip().strip('"\'') for p in mk.group(1).split(",")])
    ms = re.search(r'"summary"\s*:\s*"([^"]*)"', text, re.DOTALL)
    summary = _clean_summary(ms.group(1)) if ms else ""
    if kws or summary:
        return kws, summary

    # 3) 최후 폴백: JSON/펜스 잔재는 요약으로 노출하지 않는다(빈 요약이 raw보다 낫다)
    if _looks_like_json(text):
        return [], ""
    return [], text.strip()[:300]


# ============================================================
# 반복(loop / map-refine) 추출
# 긴 본문을 N자로 잘라 조각을 하나씩 돌리며,
# "지금까지의 키워드 + 이번 조각"을 주어 키워드를 누적하고 요약을 갱신한다.
# SLM은 컨텍스트가 길수록 품질이 떨어지므로 조각을 짧게 유지한다.
# ============================================================

PROMPT_ITER = (
    "당신은 문서 색인 비서입니다. 긴 문서 '{name}' 전체에서 균등하게 발췌한 "
    "조각들을 순서대로 읽고 있습니다(조각 사이 내용은 생략돼 있음).\n"
    "지금까지 정리한 키워드: {prev}\n"
    "[지금까지 요약]\n{prev_summary}\n"
    "아래 {i}/{n} 번째 조각을 반영해 키워드 목록을 갱신하고(기존 유지+추가), "
    "위 요약을 확장·보완하세요(기존 내용은 유지하고 새 내용을 덧붙일 것, "
    "{summary_len}. 인물·사건·주제 등 핵심을 담을 것).\n"
    "JSON으로만 답하세요. 형식: {{\"keywords\": [\"...\", \"...\"], \"summary\": \"...\"}}\n"
    "키워드는 개수 제한 없이 한국어로. 다른 말은 하지 마세요.\n\n"
    "[조각 {i}/{n}]\n{body}"
)


# 회의록 고정 템플릿 — 저사양 모델은 "어떤 구조로 쓸지" 스스로 판단하는 걸
# 가장 어려워하므로, 채울 자리(마커)를 고정해 빈칸만 메우게 한다.
# 마커 ①②③④ 는 meeting.html splitSummary 가, ' · ' 구분은 splitItems 가 파싱한다.
# (실시간 증분 요약과 종료 후 최종 정리가 이 템플릿을 공유한다)
MEETING_TEMPLATE = (
    "①개요 · <회의/강의 전체를 2~3문장으로 요약>\n"
    "②핵심 내용 · <다룬 주제마다 '주제: 무엇을 왜 어떻게'를 2~4문장으로 상세히. 여러 주제면 ' · ' 로 구분>\n"
    "③결정사항 · <합의·결정된 것. 없으면 해당 없음>\n"
    "④액션아이템 · <담당자: 할 일 (기한). 없으면 해당 없음>\n"
    "⑤미결 이슈 · <결론 못 낸 것. 없으면 해당 없음>"
)

# 형식을 눈으로 보여 주는 채워진 예시(few-shot). ' · ' 로 항목/주제를 구분한다.
# ②핵심 내용을 실제로 상세하게 채운 예시로, 약한 모델이 '뭉뚱그리지 않는' 기준을 잡게 한다.
MEETING_EXAMPLE = (
    "①개요 · 티켓 구매 시스템에서 생기는 동시성 버그 두 가지와 데이터베이스 차원의 해결책을 설명함. "
    "②핵심 내용 · 레이스 컨디션: 두 사용자가 거의 동시에 남은 재고를 읽고 각자 1을 빼서, 재고가 하나뿐인데 같은 티켓이 두 번 팔림 · "
    "부분 쓰기: 재고 차감은 됐는데 주문 생성 전에 서버가 죽으면 티켓이 사라짐 · "
    "원자적 연산: 재고>0일 때만 차감하는 단일 UPDATE로 읽기-쓰기 간극을 없애 레이스 컨디션 해결 · "
    "트랜잭션: 여러 작업을 묶어 전부 성공 아니면 전부 롤백해 부분 쓰기 방지 · "
    "로우 락(SELECT FOR UPDATE): 한 행을 잠가 다른 요청이 끝날 때까지 읽기·쓰기를 막음 "
    "③결정사항 · 해당 없음 ④액션아이템 · 해당 없음 ⑤미결 이슈 · 해당 없음"
)

MEETING_RULES = (
    "규칙: 전사에 실제로 나온 내용만, 추측·창작은 절대 금지. 각 섹션 항목은 ' · ' 로 구분. "
    "②핵심 내용은 최대한 구체적으로(주제마다 예시·메커니즘까지 풀어서) 쓰고 한 줄로 뭉뚱그리지 말 것. "
    "결정·액션·미결이 없으면 그 섹션에 '해당 없음' 이라고만 쓰세요. "
    "summary 는 다섯 마커(①②③④⑤)를 이 순서 그대로 포함해야 합니다."
)

PROMPT_ITER_MEETING = (
    "당신은 회의록 작성 비서입니다. 회의/강의 전사본 '{name}' 을 여러 조각으로 나눠 "
    "순서대로 읽고 있습니다.\n"
    "[지금까지 작성한 회의록]\n{prev_summary}\n\n"
    "아래 {i}/{n} 번째 조각을 반영해 회의록을 갱신하세요. 기존 내용은 반드시 유지하면서 "
    "새 내용을 덧붙이고(앞 조각 내용을 절대 지우지 말 것), 키워드도 갱신하세요"
    "(참석자·핵심 용어). 지금까지 키워드: {prev}\n"
    "요약은 반드시 아래 템플릿의 다섯 섹션을 이 순서·이 마커 그대로 채웁니다:\n"
    "{template}\n"
    "{rules}\n"
    "예시 summary: \"{example}\"\n"
    "JSON으로만 답하세요. 형식: {{\"keywords\": [\"...\", \"...\"], \"summary\": \"...\"}}\n"
    "키워드는 한국어로. 다른 말은 하지 마세요.\n\n"
    "[조각 {i}/{n}]\n{body}"
)


def build_iter_prompt(name, prev_keywords, prev_summary, body, i, n, mode="doc"):
    prev = ", ".join(prev_keywords) if prev_keywords else "(아직 없음)"
    ps = prev_summary or "(아직 없음)"
    if mode == "meeting":
        return PROMPT_ITER_MEETING.format(prev=prev, prev_summary=ps, name=name, i=i, n=n,
                                          template=MEETING_TEMPLATE,
                                          example=MEETING_EXAMPLE,
                                          rules=MEETING_RULES,
                                          body=body or "(빈 조각)")
    # 조각이 여럿인 긴 문서는 한두 문장으로는 전체를 담을 수 없다
    summary_len = "6~10문장" if n > 1 else "3~6문장"
    return PROMPT_ITER.format(prev=prev, prev_summary=ps, name=name, i=i, n=n,
                              summary_len=summary_len, body=body or "(빈 조각)")


# 실시간(증분) 회의록 — 회의 도중 주기적으로 "지금까지 회의록 + 새 발화"를 주고
# 갱신본을 받는다. 종료 후 정리와 같은 템플릿을 공유해 형식을 일치시킨다.
PROMPT_LIVE_MINUTES = (
    "당신은 회의록을 실시간으로 갱신하는 비서입니다.\n"
    "아래는 지금까지 작성된 회의록 초안과, 방금 새로 나온 발화입니다.\n"
    "새 발화를 반영해 회의록을 갱신하세요(기존 항목은 유지·보완하고, 새 내용을 추가).\n"
    "요약은 반드시 아래 템플릿의 다섯 섹션을 이 순서·이 마커 그대로 채웁니다:\n"
    "{template}\n"
    "{rules}\n"
    "예시 summary: \"{example}\"\n"
    "JSON으로만 답하세요. 형식: {{\"keywords\": [\"...\"], \"summary\": \"...\"}}\n"
    "키워드는 한국어로. 다른 말은 하지 마세요.\n\n"
    "[지금까지 회의록 초안]\n{current}\n\n[새 발화]\n{delta}"
)


def build_live_minutes_prompt(current_minutes, delta):
    """실시간 증분 회의록 갱신 프롬프트. current_minutes(현재 요약)와
    delta(새로 확정된 발화 묶음)를 주고 갱신본을 요청한다."""
    return PROMPT_LIVE_MINUTES.format(
        template=MEETING_TEMPLATE, rules=MEETING_RULES, example=MEETING_EXAMPLE,
        current=(current_minutes or "(아직 없음)"),
        delta=(delta or "(없음)"))


def split_body(body, size):
    """본문을 size(문자) 단위로 자른다. 빈 본문은 빈 리스트."""
    body = (body or "").strip()
    if not body:
        return []
    if size <= 0 or len(body) <= size:
        return [body]
    return [body[i:i + size] for i in range(0, len(body), size)]


def sample_chunks(chunks, max_parts):
    """조각이 max_parts를 넘으면 문서 전체에서 균등 간격으로 뽑는다.

    종전의 chunks[:max_parts]는 긴 문서(소설 등)의 앞부분만 읽어
    '도입부 요약'이 전체 요약으로 저장되는 문제가 있었다.
    첫 조각과 마지막 조각은 항상 포함한다(도입·결말).
    """
    n = len(chunks)
    if n <= max_parts:
        return chunks
    if max_parts <= 1:
        return chunks[:1]
    step = (n - 1) / (max_parts - 1)
    idxs = sorted({round(i * step) for i in range(max_parts)})
    return [chunks[i] for i in idxs]


def merge_keywords(prev, new):
    """대소문자 무시 중복 제거, 순서 유지로 키워드 누적."""
    seen = {k.lower(): True for k in prev}
    out = list(prev)
    for k in new:
        lk = k.lower()
        if lk not in seen:
            seen[lk] = True
            out.append(k)
    return out


def run_text_extraction(chat, name, body, *, chunk_size=2000, max_parts=12,
                        on_progress=None, mode="doc"):
    """본문을 조각내어 반복 추출. chat(prompt)->str 호출.
    반환: (keywords, summary). 조각이 1개면 사실상 단일 호출과 동일.
    mode="meeting" 이면 회의록(안건·결정·액션아이템) 중심으로 정리한다.
    """
    chunks = split_body(body, chunk_size)
    if not chunks:
        return [], ""
    chunks = sample_chunks(chunks, max_parts)  # 앞부분만이 아니라 전체 균등 발췌
    n = len(chunks)
    keywords, summary = [], ""
    for idx, chunk in enumerate(chunks, 1):
        if on_progress:
            on_progress(idx, n)
        raw = chat(build_iter_prompt(name, keywords, summary, chunk, idx, n, mode=mode))
        kws, s = parse_result(raw)
        keywords = merge_keywords(keywords, kws)
        if s:
            summary = s
    return keywords, summary
