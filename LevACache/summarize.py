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


def build_prompt(name, kind, body=""):
    if kind == "images":
        return PROMPT_VISION.format(name=name)
    return PROMPT_TEXT.format(name=name, body=body or "(본문 없음)")


def parse_result(raw):
    """모델 응답에서 keywords/summary 추출. JSON 실패 시 best-effort."""
    if not raw:
        return [], ""
    # 코드펜스/잡텍스트 안의 JSON 블록 추출
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            kws = obj.get("keywords") or []
            if isinstance(kws, str):
                kws = [k.strip() for k in kws.split(",") if k.strip()]
            kws = [str(k).strip() for k in kws if str(k).strip()][:12]
            summary = str(obj.get("summary") or "").strip()
            return kws, summary
        except Exception:
            pass
    # 폴백: 첫 줄을 요약으로
    text = raw.strip()
    return [], text[:300]
