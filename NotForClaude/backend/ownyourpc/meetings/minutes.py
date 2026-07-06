"""Meeting minutes: SLM structured summary + PDF rendering.

The SLM is asked to return a strict JSON structure (title, attendees,
summary, decisions, action_items). We parse defensively. The PDF is
rendered with reportlab and includes summary, action items, and the full
transcript.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from ..config import Settings, MEETINGS_DIR
from ..llm import OllamaProvider
from .transcribe import Transcript

SUMMARY_SYSTEM = (
    "당신은 회의록 작성 전문가입니다. 아래 전사 내용을 바탕으로 "
    "반드시 JSON만 출력하세요. 전사에 없는 내용은 추측하지 마세요. "
    "스키마: {\"title\": str(회의 핵심 주제 제목, 20자 이내), "
    "\"summary\": str, \"key_topics\": [str], "
    "\"decisions\": [str], \"action_items\": "
    "[{\"task\": str, \"owner\": str, \"due\": str}]}"
)

TITLE_SYSTEM = (
    "다음 회의 전사의 핵심 주제를 나타내는 간결한 제목만 한 줄로 출력하세요. "
    "20자 이내, 따옴표·마침표·설명 없이 제목 텍스트만."
)


def suggest_title(text: str, provider: OllamaProvider) -> str:
    """Quick topic-title from the (partial) transcript — for live updating."""
    snippet = (text or "").strip()[:4000]
    if len(snippet) < 10:
        return ""
    raw = provider.generate(f"# 전사\n{snippet}\n\n# 제목", system=TITLE_SYSTEM,
                            temperature=0.3)
    line = (raw or "").strip().splitlines()[0] if raw.strip() else ""
    return line.strip().strip('"').strip("'").strip("#").strip()[:30]


LIVE_SUMMARY_SYSTEM = (
    "다음은 진행 중인 회의의 전사입니다. 지금까지의 내용을 3~6개의 간결한 "
    "마크다운 불릿(- )으로 요약하세요. 핵심 논의·결정·할 일 위주로, "
    "불릿만 출력하고 군더더기·서두 없이 작성하세요. 전사에 없는 내용은 추측 금지.")


def live_summary(text: str, provider: OllamaProvider) -> str:
    """SLM running summary of the meeting so far (for the live doc panel)."""
    snippet = (text or "").strip()[-8000:]
    if len(snippet) < 30:
        return ""
    return provider.generate(f"# 전사\n{snippet}\n\n# 요약 (불릿만)",
                             system=LIVE_SUMMARY_SYSTEM, temperature=0.3).strip()


@dataclass
class Minutes:
    title: str
    summary: str = ""
    key_topics: List[str] = field(default_factory=list)
    decisions: List[str] = field(default_factory=list)
    action_items: List[dict] = field(default_factory=list)


def summarize(transcript: Transcript, settings: Settings,
              provider: OllamaProvider) -> Minutes:
    text = transcript.full_text()[:24000]  # guard context
    prompt = f"# 회의 제목\n{transcript.title}\n\n# 전사\n{text}\n\n# JSON 회의록"
    raw = provider.generate(prompt, system=SUMMARY_SYSTEM, temperature=0.2)
    data = _extract_json(raw)
    return Minutes(
        title=(data.get("title") or transcript.title or "회의").strip()[:30],
        summary=data.get("summary", ""),
        key_topics=data.get("key_topics", []),
        decisions=data.get("decisions", []),
        action_items=data.get("action_items", []),
    )


def _extract_json(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def render_pdf(minutes: Minutes, transcript: Transcript,
               out_path: Path | None = None) -> Path:
    """Render a polished meeting-minutes PDF with reportlab."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    ListFlowable, ListItem, Table, TableStyle)
    from reportlab.lib import colors
    import time as _time

    out_path = out_path or (MEETINGS_DIR / f"{transcript.meeting_id}_minutes.pdf")
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("Body2", parent=styles["BodyText"], leading=15))
    doc = SimpleDocTemplate(str(out_path), pagesize=A4,
                            topMargin=2 * cm, bottomMargin=2 * cm)
    story = []

    story.append(Paragraph(f"회의록: {minutes.title}", styles["Title"]))
    dt = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(transcript.started_at))
    story.append(Paragraph(f"일시: {dt}", styles["Normal"]))
    story.append(Spacer(1, 12))

    story.append(Paragraph("요약", styles["Heading2"]))
    story.append(Paragraph(minutes.summary or "(요약 없음)", styles["Body2"]))
    story.append(Spacer(1, 8))

    if minutes.key_topics:
        story.append(Paragraph("주요 주제", styles["Heading2"]))
        story.append(ListFlowable(
            [ListItem(Paragraph(t, styles["Body2"])) for t in minutes.key_topics],
            bulletType="bullet"))
        story.append(Spacer(1, 8))

    if minutes.decisions:
        story.append(Paragraph("결정 사항", styles["Heading2"]))
        story.append(ListFlowable(
            [ListItem(Paragraph(d, styles["Body2"])) for d in minutes.decisions],
            bulletType="bullet"))
        story.append(Spacer(1, 8))

    if minutes.action_items:
        story.append(Paragraph("액션 아이템", styles["Heading2"]))
        rows = [["할 일", "담당", "기한"]]
        for a in minutes.action_items:
            rows.append([a.get("task", ""), a.get("owner", ""), a.get("due", "")])
        tbl = Table(rows, colWidths=[9 * cm, 4 * cm, 3 * cm])
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2d3748")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 8))

    doc.build(story)
    return out_path


# ---------------------------------------------------------------------------
# Markdown document + HTML-template PDF
# ---------------------------------------------------------------------------
def _mesc(s: str) -> str:
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_markdown(minutes: "Minutes", transcript: Transcript, when: str = "") -> str:
    """Simple, readable Markdown meeting document from the transcript summary."""
    out: List[str] = [f"# {minutes.title}", ""]
    if when:
        out += [f"*{when}*", ""]
    out += ["## 요약", minutes.summary or "(요약 없음)", ""]
    if minutes.key_topics:
        out += ["## 주요 주제"] + [f"- {t}" for t in minutes.key_topics] + [""]
    if minutes.decisions:
        out += ["## 결정 사항"] + [f"- {d}" for d in minutes.decisions] + [""]
    if minutes.action_items:
        out += ["## 액션 아이템", "", "| 할 일 | 담당 | 기한 |", "|---|---|---|"]
        for a in minutes.action_items:
            out.append(f"| {a.get('task','')} | {a.get('owner','')} | {a.get('due','')} |")
        out.append("")
    return "\n".join(out)


def render_html(minutes: "Minutes", transcript: Transcript, when: str = "") -> str:
    """Meeting-minutes HTML template (styled for print → PDF)."""
    topics = "".join(f"<li>{_mesc(t)}</li>" for t in minutes.key_topics)
    decisions = "".join(f"<li>{_mesc(d)}</li>" for d in minutes.decisions)
    ai_rows = "".join(
        f"<tr><td>{_mesc(a.get('task',''))}</td><td>{_mesc(a.get('owner',''))}</td>"
        f"<td>{_mesc(a.get('due',''))}</td></tr>" for a in minutes.action_items)
    topics_block = f"<h2>주요 주제</h2><ul>{topics}</ul>" if topics else ""
    dec_block = f"<h2>결정 사항</h2><ul>{decisions}</ul>" if decisions else ""
    ai_block = (f"<h2>액션 아이템</h2><table><thead><tr><th>할 일</th><th>담당</th>"
                f"<th>기한</th></tr></thead><tbody>{ai_rows}</tbody></table>") if ai_rows else ""
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
@page {{ size: A4; margin: 2cm; }}
body {{ font-family: "HYSMyeongJo-Medium"; color: #1a202c; font-size: 11px; line-height: 1.6; }}
.head {{ border-bottom: 2px solid #2563eb; padding-bottom: 8px; margin-bottom: 16px; }}
h1 {{ font-size: 20px; margin: 0; color: #111827; }}
.date {{ color: #718096; font-size: 10px; margin-top: 3px; }}
h2 {{ font-size: 13px; color: #2563eb; border-bottom: 1px solid #e2e8f0;
     padding-bottom: 3px; margin-top: 18px; }}
ul {{ margin: 6px 0; }} li {{ margin: 2px 0; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 6px; }}
th {{ background: #2d3748; color: #ffffff; padding: 5px 7px; font-size: 10px; text-align: left; }}
td {{ border: 1px solid #cbd5e0; padding: 5px 7px; font-size: 10px; }}
.summary {{ background: #f7fafc; padding: 10px 12px; }}
.seg {{ margin: 3px 0; }} .spk {{ color: #2563eb; }}
</style></head><body>
<div class="head"><h1>{_mesc(minutes.title)}</h1><div class="date">{_mesc(when)}</div></div>
<h2>요약</h2><div class="summary">{_mesc(minutes.summary) or "(요약 없음)"}</div>
{topics_block}{dec_block}{ai_block}
</body></html>"""


def html_to_pdf(html: str, out_path: Path) -> Path:
    """Render HTML → PDF via xhtml2pdf, with a Korean CID font registered."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    except Exception:
        pass
    from xhtml2pdf import pisa
    with open(out_path, "wb") as f:
        pisa.CreatePDF(html, dest=f, encoding="utf-8")
    return Path(out_path)
