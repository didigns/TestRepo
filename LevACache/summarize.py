"""모델에게 키워드+요약을 JSON으로 요청하고 파싱한다."""
import json
import re

PROMPT_TEXT = (
    "당신은 문서 색인 비서입니다. 아래 문서 내용을 분석해서 "
    "핵심 키워드와 한두 문장 요약을 JSON으로만 답하세요.\n"
    "형식: {{\"keywords\": [\"...\", \"...\"], \"summary\": \"...\"}}\n"
    "키워드는 5~8개, 한국어로. 다른 말은 하지 마세요.\n\n"
    "[문서: {name}]\n{body}"
)

PROMPT_VISION = (
    "당신은 문서/이미지 색인 비서입니다. 첨부한 이미지를 보고 "
    "핵심 키워드와 한두 문장 요약을 JSON으로만 답하세요.\n"
    "형식: {{\"keywords\": [\"...\", \"...\"], \"summary\": \"...\"}}\n"
    "키워드는 5~8개, 한국어로. 다른 말은 하지 마세요.\n\n"
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
    return [str(k).strip() for k in (kws or []) if str(k).strip()][:12]


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
