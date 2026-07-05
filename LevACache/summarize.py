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


def parse_result(raw):
    """모델 응답에서 keywords/summary 추출. 사고 과정·잘린 JSON에 견고하게."""
    if not raw:
        return [], ""
    text = _strip_thinking(raw)

    # 1) 균형 JSON 객체 파싱
    obj = _extract_json_obj(text)
    if obj is not None:
        return _norm_keywords(obj.get("keywords")), str(obj.get("summary") or "").strip()

    # 2) JSON이 깨졌거나 잘린 경우 정규식으로 keywords/summary 건져내기
    kws = []
    mk = re.search(r'"keywords"\s*:\s*\[(.*?)\]', text, re.DOTALL)
    if mk:
        kws = _norm_keywords([p.strip().strip('"\'') for p in mk.group(1).split(",")])
    ms = re.search(r'"summary"\s*:\s*"([^"]*)"', text, re.DOTALL)
    summary = ms.group(1).strip() if ms else ""
    if kws or summary:
        return kws, summary

    # 3) 최후 폴백: 앞부분을 요약으로
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
    "아래는 {i}/{n} 번째 조각입니다. 이 조각의 내용을 반영해 "
    "키워드 목록을 갱신하세요(기존 키워드는 유지하고, 새로 필요한 것을 추가·보완). "
    "그리고 지금까지 읽은 내용 전체를 아우르는 요약을 갱신하세요"
    "({summary_len}. 인물·사건·주제 등 핵심을 담을 것).\n"
    "JSON으로만 답하세요. 형식: {{\"keywords\": [\"...\", \"...\"], \"summary\": \"...\"}}\n"
    "키워드는 개수 제한 없이 한국어로. 다른 말은 하지 마세요.\n\n"
    "[조각 {i}/{n}]\n{body}"
)


PROMPT_ITER_MEETING = (
    "당신은 회의록 작성 비서입니다. 회의 녹음의 전사본(타임스탬프 포함) "
    "'{name}' 을 여러 조각으로 나눠 순서대로 읽고 있습니다.\n"
    "지금까지 정리한 키워드: {prev}\n"
    "아래는 {i}/{n} 번째 조각입니다. 키워드 목록을 갱신하고"
    "(참석자·프로젝트명·핵심 주제 위주), 지금까지 내용 전체를 아우르는 "
    "회의 요약을 갱신하세요. 요약에 반드시 담을 것: ①주요 안건 ②결정사항 "
    "③액션아이템(담당자·기한이 언급됐으면 함께) ④미결 이슈. "
    "전사에 없는 내용은 절대 지어내지 마세요.\n"
    "JSON으로만 답하세요. 형식: {{\"keywords\": [\"...\", \"...\"], \"summary\": \"...\"}}\n"
    "키워드는 한국어로. 다른 말은 하지 마세요.\n\n"
    "[조각 {i}/{n}]\n{body}"
)


def build_iter_prompt(name, prev_keywords, body, i, n, mode="doc"):
    prev = ", ".join(prev_keywords) if prev_keywords else "(아직 없음)"
    if mode == "meeting":
        return PROMPT_ITER_MEETING.format(prev=prev, name=name, i=i, n=n,
                                          body=body or "(빈 조각)")
    # 조각이 여럿인 긴 문서는 한두 문장으로는 전체를 담을 수 없다
    summary_len = "4~6문장" if n > 1 else "한두 문장"
    return PROMPT_ITER.format(prev=prev, name=name, i=i, n=n,
                              summary_len=summary_len, body=body or "(빈 조각)")


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
        raw = chat(build_iter_prompt(name, keywords, chunk, idx, n, mode=mode))
        kws, s = parse_result(raw)
        keywords = merge_keywords(keywords, kws)
        if s:
            summary = s
    return keywords, summary
