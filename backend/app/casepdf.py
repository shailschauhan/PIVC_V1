"""A printable report for one video: the decision, a written interpretation (if one was generated), each test's result and the
reasons behind it, the human review, the rules in force, and the activity trail. Built to be read by a reviewer or filed as a record.

It names no engine or model. Text is limited to what the built-in PDF fonts can draw, so typographic characters are simplified.
"""
import io
import re
from datetime import datetime, timezone

from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import CondPageBreak, HRFlowable, KeepTogether, ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import parameters
from .db import Row

INK, MUTED, LINE, PAPER = colors.HexColor("#1f2937"), colors.HexColor("#6b7280"), colors.HexColor("#d9dde3"), colors.HexColor("#f6f7f9")
BRAND = colors.HexColor("#0b5cad")
TONES = {"good": (colors.HexColor("#1a7f4b"), colors.HexColor("#e2f5ea")), "warn": (colors.HexColor("#9a6200"), colors.HexColor("#fff3d6")),
         "bad": (colors.HexColor("#c0392b"), colors.HexColor("#fbe7e4")), "muted": (colors.HexColor("#6b7280"), colors.HexColor("#eef0f3"))}
DECISION_TONE = {"Accept": "good", "Needs Review": "warn", "Reject": "bad"}
LEVEL_TONE = {"ok": "good", "attention": "warn", "info": "muted"}
_SIMPLE = str.maketrans({"→": "->", "←": "<-", "≥": ">=", "≤": "<=", "“": '"', "”": '"', "‘": "'", "’": "'",
                         "–": "-", "—": "-", "…": "...", "×": "x", "•": "-", " ": " ", "−": "-"})


def clean(v) -> str:
    """Plain latin-1 text that is safe inside a Paragraph (the built-in fonts cannot draw everything, and < & are markup)."""
    s = str(v if v is not None else "").translate(_SIMPLE)
    s = s.encode("latin-1", "replace").decode("latin-1")
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tone(key: str, status: str) -> str:
    spec = parameters.BY_KEY.get(key)
    return next((t for s, t in (spec["outcomes"] if spec else []) if s == status), "muted")


def _fmt_dt(d) -> str:
    return d.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC") if isinstance(d, datetime) else "-"


def _num(v, nd=2) -> str:
    return "-" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))


def _readings(key: str, d: dict) -> list[tuple[str, str]]:
    """The few numbers that matter for each test, in words a reviewer knows."""
    if key == "video_authenticity":
        out = [("Manipulation score", _num(d.get("fake_score"), 3) + " (0 = clean, 1 = manipulated)"),
               ("Frames with a face", f"{d.get('frames_with_face', '-')} of {d.get('frames_sampled', '-')} examined"),
               ("Frames above the flag threshold", str(d.get("suspicious_frames", "-")))]
        bg = d.get("background") or {}
        out.append(("Background", ("virtual or blurred suspected: " + ", ".join(bg.get("reasons", []))) if bg.get("virtual_suspected")
                    else ("looks natural" if bg.get("assessed") else "not assessed")))
        return out
    if key == "health_indicators":
        out = [("Heart rate", f"{_num(d.get('hr_bpm'), 0)} bpm"), ("Breathing rate", f"{_num(d.get('breathing_rate_bpm'), 0)} per minute"),
               ("Signal quality", _num(d.get("sqi"))), ("Video length", f"{_num(d.get('duration_s'), 0)} s")]
        if d.get("not_reliable_because"):
            out.append(("Why not reliable", "; ".join(d["not_reliable_because"])))
        return out
    if key == "age_estimation":
        out = [("Estimated age", f"{_num(d.get('age'), 0)}" + (f" (range {_num(d.get('age_low'), 0)} to {_num(d.get('age_high'), 0)})" if d.get("age_low") is not None else "")),
               ("Declared age", _num(d.get("declared_age"), 0) if d.get("declared_age") is not None else "not supplied")]
        if d.get("difference") is not None:
            out.append(("Estimated minus declared", f"{d['difference']:+.0f} years"))
        if d.get("not_reliable_because"):
            out.append(("Why not reliable", "; ".join(d["not_reliable_because"])))
        return out
    if key == "face_match":
        if d.get("skipped"):
            return [("Not checked", d["skipped"])]
        return [("Similarity to the ID photo", _num(d.get("similarity"), 3) + " (0 to 1)"), ("Frames compared", str(d.get("frames_usable", "-"))),
                ("Outcome", str(d.get("decision", "-")).replace("_", " "))] + ([("Note", d["reason"])] if d.get("reason") else [])
    if key == "liveness":
        return [("Probability the face is real", _num(d.get("real_probability"), 3) + " (0 to 1)"), ("Frames judged", str(d.get("frames_usable", "-"))),
                ("Presentation attack suspected", "yes" if d.get("spoof_suspected") else "no")] + ([("Note", d["reason"])] if d.get("reason") else [])
    return []


class _Numbered(canvas.Canvas):
    """Draws 'Page x of y' and the footer once the page count is known."""
    footer = ""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._states = []

    def showPage(self):
        self._states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._states)
        for st in self._states:
            self.__dict__.update(st)
            self.setStrokeColor(LINE)
            self.line(15 * mm, 13 * mm, A4[0] - 15 * mm, 13 * mm)
            self.setFont("Helvetica", 7.5)
            self.setFillColor(MUTED)
            self.drawString(15 * mm, 8.5 * mm, self.footer)
            self.drawRightString(A4[0] - 15 * mm, 8.5 * mm, f"Page {self._pageNumber} of {total}")
            super().showPage()
        super().save()


def _styles() -> dict:
    ss = getSampleStyleSheet()
    base = ParagraphStyle("base", parent=ss["BodyText"], fontName="Helvetica", fontSize=9.5, leading=13.5, textColor=INK)
    return {
        "base": base, "small": ParagraphStyle("small", parent=base, fontSize=8, leading=11, textColor=MUTED),
        "h1": ParagraphStyle("h1", parent=base, fontName="Helvetica-Bold", fontSize=19, leading=23),
        "h2": ParagraphStyle("h2", parent=base, fontName="Helvetica-Bold", fontSize=12, leading=16, spaceBefore=12, spaceAfter=5, textColor=BRAND),
        "h3": ParagraphStyle("h3", parent=base, fontName="Helvetica-Bold", fontSize=10.5, leading=14),
        "lead": ParagraphStyle("lead", parent=base, fontName="Helvetica-Bold", fontSize=11.5, leading=16),
        "cell": ParagraphStyle("cell", parent=base, fontSize=8.5, leading=11.5, alignment=TA_LEFT),
        "label": ParagraphStyle("label", parent=base, fontSize=7.5, leading=10, textColor=MUTED),
    }


def _chip(text: str, tone: str, st: dict) -> Paragraph:
    fg, _bg = TONES[tone]
    return Paragraph(f'<font color="{fg.hexval()}"><b>{clean(text)}</b></font>', st["cell"])


def _frame_chart(frames: list[dict], flag: float, review: float, width: float) -> Drawing:
    """One bar per examined frame, coloured by how it compares with the review and flag thresholds."""
    h, pad, right = 38 * mm, 8, 34
    d = Drawing(width, h + 12)
    d.add(Rect(0, 12, width, h, fillColor=PAPER, strokeColor=LINE, strokeWidth=0.5))
    n = max(len(frames), 1)
    bw = (width - pad - right) / n
    for i, f in enumerate(frames):
        s = max(0.0, min(1.0, float(f.get("score", 0))))
        col = TONES["bad"][0] if s >= flag else TONES["warn"][0] if s >= review else TONES["good"][0]
        d.add(Rect(pad + i * bw + 0.4, 12 + 3, max(bw - 0.8, 0.6), max(s * (h - 8), 0.8), fillColor=col, strokeColor=None))
    for val, lbl in ((flag, "flag"), (review, "review")):
        y = 12 + 3 + val * (h - 8)
        d.add(Line(pad, y, width - right, y, strokeColor=MUTED, strokeWidth=0.6, strokeDashArray=[3, 2]))
        d.add(String(width - right + 3, y - 2.5, lbl, fontSize=6.5, fillColor=MUTED))
    d.add(String(pad, 2, "start of video", fontSize=6.5, fillColor=MUTED))
    d.add(String(width - right, 2, "end of video", fontSize=6.5, fillColor=MUTED, textAnchor="end"))
    return d


def _box(flow: list, bg, border, width: float, pad: int = 8) -> Table:
    t = Table([[flow]], colWidths=[width])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), bg), ("BOX", (0, 0), (-1, -1), 0.6, border), ("LEFTPADDING", (0, 0), (-1, -1), pad + 2),
                           ("RIGHTPADDING", (0, 0), (-1, -1), pad + 2), ("TOPPADDING", (0, 0), (-1, -1), pad), ("BOTTOMPADDING", (0, 0), (-1, -1), pad)]))
    return t


def _bullets(items: list[str], st: dict) -> ListFlowable:
    return ListFlowable([ListItem(Paragraph(clean(i), st["base"]), leftIndent=12) for i in items], bulletType="bullet", start="-", leftIndent=12, bulletFontSize=8)


def build(case: Row, audit: list[Row], interpretation: dict | None, who: str, filename: str) -> bytes:
    st = _styles()
    rep = case.report or {}
    params = rep.get("parameters") or {}
    rules = rep.get("rules_applied") or {}
    width = A4[0] - 30 * mm
    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm, bottomMargin=18 * mm,
                            title=f"Video analysis report {case.case_ref}", author=who)
    _Numbered.footer = f"{case.case_ref}  |  Confidential  |  Decision support only: scores are not calibrated probabilities"
    flow: list = []

    # ---- title
    head = Table([[Paragraph("Video analysis report", st["h1"]), Paragraph(clean(case.case_ref), ParagraphStyle("r", parent=st["h3"], alignment=2, textColor=BRAND))]],
                 colWidths=[width * 0.65, width * 0.35])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    flow += [head, HRFlowable(width="100%", thickness=1.4, color=BRAND, spaceBefore=3, spaceAfter=6)]
    meta = [("File", filename), ("Reference", case.proposal_no if case.proposal_no != "-" else "none"), ("Uploaded", _fmt_dt(case.created_at)),
            ("Analysed", _fmt_dt(case.analyzed_at)), ("Report created", f"{_fmt_dt(datetime.now(timezone.utc))} by {who}")]
    mt = Table([[Paragraph(f"<b>{clean(k)}</b>", st["cell"]), Paragraph(clean(v), st["cell"])] for k, v in meta], colWidths=[28 * mm, width - 28 * mm])
    mt.setStyle(TableStyle([("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    flow += [mt, Spacer(1, 6)]

    # ---- decision
    dec = case.decision or "Not decided"
    fg, bg = TONES[DECISION_TONE.get(dec, "muted")]
    reviewed = case.review_status == "COMPLETED"
    review_line = (f"Decided by a reviewer: <b>{clean(case.final_decision)}</b> ({clean(case.reviewer)}, {_fmt_dt(case.reviewed_at)})" if reviewed
                   else "Waiting for a reviewer" if case.review_status == "PENDING" else "Decided automatically; no review was required")
    cells = [[Paragraph("AUTOMATIC DECISION", st["label"]), Paragraph("CONFIDENCE", st["label"]), Paragraph("REVIEW", st["label"])],
             [Paragraph(f'<font color="{fg.hexval()}" size="16"><b>{clean(dec)}</b></font>', st["base"]),
              Paragraph(f"<font size=\"16\"><b>{(case.confidence or 0):.0%}</b></font>", st["base"]), Paragraph(review_line, st["cell"])]]
    dt = Table(cells, colWidths=[width * 0.32, width * 0.2, width * 0.48])
    dt.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    flow += [_box([dt], bg, fg, width)]
    label = {k: p["label"] for k, p in parameters.BY_KEY.items()}
    reasons = [re.sub(r"^\[(\w+)\]\s*", lambda m: label.get(m.group(1), m.group(1)) + ": ", r) for r in rep.get("exception_details") or []]
    if reasons:
        flow += [Spacer(1, 6), Paragraph("Why it needs attention", st["h3"]), _bullets(reasons, st)]

    # ---- interpretation
    if interpretation:
        flow += [Paragraph("Interpretation", st["h2"])]
        body: list = [Paragraph(clean(interpretation.get("headline")), st["lead"]), Spacer(1, 3), Paragraph(clean(interpretation.get("summary")), st["base"])]
        if interpretation.get("stale"):
            body += [Spacer(1, 3), Paragraph("<b>Note:</b> written for an earlier version of these results; some tests were re-run since.", st["small"])]
        flow += [_box(body, colors.HexColor("#eef4fb"), colors.HexColor("#c9dcf2"), width)]
        for f in interpretation.get("findings") or []:
            fg2, _ = TONES[LEVEL_TONE.get(f.get("level"), "muted")]
            flow += [Spacer(1, 3), Paragraph(f'<font color="{fg2.hexval()}"><b>{clean(f.get("test"))}</b></font>: {clean(f.get("text"))}', st["base"])]
        for title, key in (("To look at", "concerns"), ("Suggested next steps", "next_steps"), ("What these results cannot tell", "caveats")):
            if interpretation.get(key):
                flow += [Spacer(1, 5), Paragraph(title, st["h3"]), _bullets(interpretation[key], st)]
        flow += [Spacer(1, 4), Paragraph("Written by an AI assistant from the results in this report. It explains them; it does not change the decision and is not a "
                                         "further test. The reviewer decides.", st["small"])]

    # ---- at a glance
    flow += [CondPageBreak(70 * mm), Paragraph("Results at a glance", st["h2"])]
    rows = [[Paragraph("<b>Test</b>", st["cell"]), Paragraph("<b>Result</b>", st["cell"]), Paragraph("<b>Confidence</b>", st["cell"]), Paragraph("<b>In short</b>", st["cell"])]]
    for spec in parameters.PARAMETERS:
        p = params.get(spec["key"])
        if p:
            rows.append([Paragraph(clean(spec["label"]), st["cell"]), _chip(p["status"], _tone(spec["key"], p["status"]), st),
                         Paragraph(f"{p.get('confidence', 0):.0%}", st["cell"]), Paragraph(clean(p.get("summary")), st["cell"])])
    gt = Table(rows, colWidths=[34 * mm, 34 * mm, 22 * mm, width - 90 * mm], repeatRows=1)
    gt.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), PAPER), ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("LEFTPADDING", (0, 0), (-1, -1), 4)]))
    flow += [gt]

    # ---- each test
    flow += [Paragraph("The tests in detail", st["h2"])]
    for spec in parameters.PARAMETERS:
        p = params.get(spec["key"])
        if not p:
            continue
        d = p.get("details") or {}
        fgt, bgt = TONES[_tone(spec["key"], p["status"])]
        card: list = [Table([[Paragraph(f"<b>{clean(spec['label'])}</b>", st["h3"]), _chip(p["status"], _tone(spec["key"], p["status"]), st)]],
                            colWidths=[width * 0.6, width * 0.35], style=[("LEFTPADDING", (0, 0), (-1, -1), 0), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]),
                      Paragraph(clean(spec["question"]), st["small"]), Spacer(1, 3), Paragraph(clean(p.get("summary")), st["base"])]
        rd = _readings(spec["key"], d)
        if rd and not d.get("error"):
            rt = Table([[Paragraph(clean(k), st["cell"]), Paragraph(f"<b>{clean(v)}</b>", st["cell"])] for k, v in rd], colWidths=[55 * mm, width - 55 * mm - 18])
            rt.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.3, LINE), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
            card += [Spacer(1, 4), rt]
        if d.get("error"):
            card += [Spacer(1, 3), Paragraph(f'<font color="{TONES["bad"][0].hexval()}">This test could not be completed.</font>', st["cell"])]
        if spec["key"] == "video_authenticity" and d.get("frame_results"):
            card += [Spacer(1, 5), Paragraph("Score of each examined frame", st["label"]),
                     _frame_chart(d["frame_results"], float(rules.get("fake_score_flag", 0.3)), float(rules.get("fake_score_review", 0.15)), width - 18)]
        if p.get("exceptions"):
            card += [Spacer(1, 4), _bullets(p["exceptions"], st)]
        if d.get("limitations"):
            card += [Spacer(1, 4), Paragraph("Limits: " + clean(" ".join(d["limitations"]).replace(" (rPPG)", "")), st["small"])]
        flow += [KeepTogether([_box(card, colors.white, LINE, width), Spacer(1, 6)])]

    # ---- review
    flow += [Paragraph("Human review", st["h2"])]
    if reviewed:
        flow += [Paragraph(f"Final decision <b>{clean(case.final_decision)}</b> by <b>{clean(case.reviewer)}</b> on {_fmt_dt(case.reviewed_at)}.", st["base"])]
        if case.review_notes:
            flow += [Spacer(1, 3), Paragraph("Notes: " + clean(case.review_notes), st["base"])]
    else:
        flow += [Paragraph(review_line.replace("<b>", "").replace("</b>", "") + ".", st["base"])]

    # ---- rules
    shown = [("fake_score_review", "Authenticity: review from score"), ("fake_score_flag", "Authenticity: flag from score"), ("confidence_threshold", "Minimum confidence to decide automatically"),
             ("face_match_threshold", "Face match: match at or above"), ("face_mismatch_threshold", "Face match: mismatch below"),
             ("liveness_pass_threshold", "Liveness: pass at or above"), ("liveness_spoof_threshold", "Liveness: suspected attack below"),
             ("age_tolerance_years", "Age: tolerance (years)")]
    rr = [[Paragraph(clean(lbl), st["cell"]), Paragraph(f"<b>{rules[k]}</b>", st["cell"])] for k, lbl in shown if k in rules]
    if rr:
        flow += [Paragraph("Rules applied", st["h2"]), Paragraph("The thresholds in force when this video was analysed.", st["small"]), Spacer(1, 3)]
        half = (len(rr) + 1) // 2
        cols = [rr[:half], rr[half:] + [["", ""]] * (half - len(rr[half:]))]
        rt2 = Table([[cols[0][i][0], cols[0][i][1], cols[1][i][0], cols[1][i][1]] for i in range(half)], colWidths=[width * 0.31, width * 0.1, width * 0.31, width * 0.1])
        rt2.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.3, LINE), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        flow += [rt2]

    # ---- trail
    if audit:
        flow += [Paragraph("Activity", st["h2"])]
        at = Table([[Paragraph(_fmt_dt(a.at), st["cell"]), Paragraph(clean(a.actor), st["cell"]),
                     Paragraph(clean(a.action + (f": {a.detail['final_decision']}" if (a.detail or {}).get("final_decision") else "")), st["cell"])] for a in audit[-14:]],
                   colWidths=[42 * mm, 38 * mm, width - 80 * mm])
        at.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.3, LINE), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        flow += [at]

    doc.build(flow, canvasmaker=_Numbered)
    return out.getvalue()
