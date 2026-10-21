"""Exportable reports: choose a date range, decisions and test results, then download a CSV or a PDF.

The filters are applied in SQL (the per-test results live in case_params), and rows are streamed in pages, so an export of a
very large history does not load it into memory. A PDF is a readable summary plus the first PDF_ROW_CAP rows; the CSV is complete.
"""
import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import parameters
from .db import T, Db, Row, like, marks

PDF_ROW_CAP = 5000
PDF_MAX_COLUMNS = 8
DECISIONS = ("Accept", "Needs Review", "Reject")
REVIEW_STATES = {"NOT_REQUIRED": "Decided automatically", "PENDING": "Waiting for a reviewer", "COMPLETED": "Decided by a reviewer"}


class FilterError(ValueError):
    pass


@dataclass
class Filters:
    date_from: datetime | None = None
    date_to: datetime | None = None
    decision: list[str] = field(default_factory=list)
    review_status: list[str] = field(default_factory=list)
    tests: dict[str, list[str]] = field(default_factory=dict)  # test key -> statuses (any of these)
    q: str = ""


def _when(v: str | None, name: str) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise FilterError(f"{name} is not a valid date") from None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse(date_from=None, date_to=None, decision=None, review_status=None, test=None, q="") -> Filters:
    f = Filters(_when(date_from, "from"), _when(date_to, "to"), q=(q or "").strip())
    if f.date_from and f.date_to and f.date_from > f.date_to:
        raise FilterError("The start date is after the end date")
    for d in decision or []:
        if d not in DECISIONS:
            raise FilterError(f"Unknown decision: {d}")
        f.decision.append(d)
    for r in review_status or []:
        if r not in REVIEW_STATES:
            raise FilterError(f"Unknown review state: {r}")
        f.review_status.append(r)
    for t in test or []:
        key, _, status = t.partition(":")
        spec = parameters.BY_KEY.get(key)
        if not spec or status not in [o[0] for o in spec["outcomes"]]:
            raise FilterError(f"Unknown test result: {t}")
        f.tests.setdefault(key, []).append(status)
    return f


def where(f: Filters) -> tuple[str, list]:
    """The WHERE clause (over `cases c`) and its values for a set of filters."""
    sql, args = "c.status = 'ANALYZED'", []
    if f.date_from:
        sql, args = sql + " AND c.created_at >= ?", args + [f.date_from]
    if f.date_to:
        sql, args = sql + " AND c.created_at <= ?", args + [f.date_to]
    if f.decision:
        sql, args = sql + f" AND c.decision IN ({marks(len(f.decision))})", args + f.decision
    if f.review_status:
        sql, args = sql + f" AND c.review_status IN ({marks(len(f.review_status))})", args + f.review_status
    if f.q:
        pat = like(f.q)
        sql = sql + " AND (c.case_ref LIKE ? ESCAPE '!' OR c.proposal_no LIKE ? ESCAPE '!' OR c.customer_name LIKE ? ESCAPE '!')"
        args = args + [pat, pat, pat]
    for key, statuses in f.tests.items():  # a video must match every chosen test (and any of that test's chosen results)
        sql += f" AND c.id IN (SELECT case_id FROM {T.case_params} WHERE parameter = ? AND status IN ({marks(len(statuses))}))"
        args = args + [key, *statuses]
    return sql, args


def count(db: Db, f: Filters) -> int:
    sql, args = where(f)
    return db.scalar(f"SELECT COUNT(*) FROM {T.cases} c WHERE {sql}", args)


def describe(f: Filters) -> list[str]:
    """The filters in words, for the top of a PDF and the audit log."""
    out = []
    if f.date_from or f.date_to:
        fmt = lambda d: d.strftime("%d %b %Y %H:%M UTC") if d else "…"  # noqa: E731
        out.append(f"Uploaded {'since ' + fmt(f.date_from) if not f.date_to else 'up to ' + fmt(f.date_to) if not f.date_from else 'between ' + fmt(f.date_from) + ' and ' + fmt(f.date_to)}")
    if f.decision:
        out.append("Automatic decision: " + ", ".join(f.decision))
    if f.review_status:
        out.append("Review: " + ", ".join(REVIEW_STATES[r] for r in f.review_status))
    for k, sts in f.tests.items():
        out.append(f"{parameters.BY_KEY[k]['label']}: " + ", ".join(sts))
    if f.q:
        out.append(f"Search: “{f.q}”")
    return out or ["No filters: all analysed videos"]


# ---- columns ----------------------------------------------------------------------------------------------------------
def _p(c: Row, key: str) -> dict:
    return ((c.report or {}).get("parameters") or {}).get(key) or {"status": "", "details": {}}


def _d(c: Row, key: str, field_: str):
    return _p(c, key)["details"].get(field_)


def _age_range(c: Row):
    d = _p(c, "age_estimation")["details"]
    return f"{d['age_low']:.0f}-{d['age_high']:.0f}" if d.get("age_low") is not None else ""


# key -> (label, group, getter)
COLUMNS: dict[str, tuple[str, str, object]] = {
    "case_ref": ("Video ID", "Video", lambda c: c.case_ref),
    "filename": ("File name", "Video", lambda c: (c.media or {}).get("original_name")),
    "reference": ("Reference", "Video", lambda c: c.proposal_no if c.proposal_no != "-" else ""),
    "uploaded_at": ("Uploaded (UTC)", "Video", lambda c: c.created_at),
    "analyzed_at": ("Analysed (UTC)", "Video", lambda c: c.analyzed_at),
    "decision": ("Automatic decision", "Decision", lambda c: c.decision),
    "confidence": ("Confidence", "Decision", lambda c: c.confidence),
    "exceptions": ("Reasons for review", "Decision", lambda c: " | ".join((c.report or {}).get("exception_details", []))),
    "review_status": ("Review state", "Decision", lambda c: REVIEW_STATES.get(c.review_status, c.review_status)),
    "final_decision": ("Final decision", "Decision", lambda c: c.final_decision),
    "reviewer": ("Reviewer", "Decision", lambda c: c.reviewer),
    "reviewed_at": ("Reviewed (UTC)", "Decision", lambda c: c.reviewed_at),
    "review_notes": ("Reviewer notes", "Decision", lambda c: c.review_notes),
    "authenticity": ("Authenticity", "Video authenticity", lambda c: _p(c, "video_authenticity")["status"]),
    "fake_score": ("Fake score", "Video authenticity", lambda c: _d(c, "video_authenticity", "fake_score")),
    "frames_with_face": ("Frames with a face", "Video authenticity", lambda c: _d(c, "video_authenticity", "frames_with_face")),
    "vitals": ("Health indicators", "Health indicators", lambda c: _p(c, "health_indicators")["status"]),
    "heart_rate": ("Heart rate (bpm)", "Health indicators", lambda c: _d(c, "health_indicators", "hr_bpm")),
    "breathing_rate": ("Breathing rate (/min)", "Health indicators", lambda c: _d(c, "health_indicators", "breathing_rate_bpm")),
    "vitals_sqi": ("Signal quality", "Health indicators", lambda c: _d(c, "health_indicators", "sqi")),
    "age_status": ("Age estimation", "Age estimation", lambda c: _p(c, "age_estimation")["status"]),
    "estimated_age": ("Estimated age", "Age estimation", lambda c: _d(c, "age_estimation", "age")),
    "declared_age": ("Declared age", "Age estimation", lambda c: _d(c, "age_estimation", "declared_age")),
    "age_range": ("Age range", "Age estimation", _age_range),
    "face_match": ("Face match", "Face match", lambda c: _p(c, "face_match")["status"]),
    "face_similarity": ("Face similarity", "Face match", lambda c: _d(c, "face_match", "similarity")),
    "liveness": ("Liveness", "Liveness", lambda c: _p(c, "liveness")["status"]),
    "liveness_probability": ("Real-face probability", "Liveness", lambda c: _d(c, "liveness", "real_probability")),
}
DEFAULT_COLUMNS = list(COLUMNS)
# the original /api/reports/export.csv headers and order, kept for anything already reading that file
LEGACY = {"case_ref": "case_ref", "filename": "filename", "reference": "reference", "authenticity": "authenticity", "fake_score": "fake_score",
          "frames_with_face": "frames_with_face", "decision": "overall_decision", "confidence": "confidence_score", "exceptions": "exception_details",
          "review_status": "review_status", "final_decision": "final_decision", "reviewer": "reviewer", "analyzed_at": "analyzed_at",
          "vitals": "vitals_status", "heart_rate": "heart_rate_bpm", "breathing_rate": "breathing_rate_bpm", "vitals_sqi": "vitals_sqi",
          "age_status": "age_status", "estimated_age": "estimated_age", "declared_age": "declared_age", "age_range": "age_range",
          "face_match": "face_match_status", "face_similarity": "face_similarity", "liveness": "liveness_status",
          "liveness_probability": "liveness_real_probability"}
PDF_DEFAULT = ["case_ref", "filename", "uploaded_at", "decision", "final_decision", "authenticity", "face_match", "liveness"]


def pick_columns(keys: list[str] | None, limit: int | None = None) -> list[str]:
    keys = [k for k in (keys or []) if k in COLUMNS] or (PDF_DEFAULT if limit else DEFAULT_COLUMNS)
    return list(dict.fromkeys(keys))[:limit]


def _cell(v):
    """CSV/Excel treat a leading = + - @ as a formula, and file names come from users, so neutralise them."""
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return "" if v is None else v


def rows(db: Db, f: Filters, keys: list[str], cap: int | None = None):
    sql, args = where(f)
    top = f"TOP ({int(cap)}) " if cap else ""
    for c in db.stream(f"SELECT {top}c.* FROM {T.cases} c WHERE {sql} ORDER BY c.id DESC", args, "cases", 500):
        yield [COLUMNS[k][2](c) for k in keys]


def stream_csv(db: Db, f: Filters, keys: list[str], machine_header: bool = False):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([LEGACY.get(k, k) for k in keys] if machine_header else [COLUMNS[k][0] for k in keys])
    yield "﻿" + buf.getvalue()  # BOM: Excel then reads the file as UTF-8
    buf.seek(0), buf.truncate()
    n = 0
    for r in rows(db, f, keys):
        w.writerow([_cell(v) for v in r])
        n += 1
        if n % 500 == 0:
            yield buf.getvalue()
            buf.seek(0), buf.truncate()
    yield buf.getvalue()


def summary(db: Db, f: Filters) -> dict:
    sql, args = where(f)
    by_decision: dict = {}
    by_review: dict = {}
    total = 0
    for r in db.all(f"SELECT c.decision, c.review_status, COUNT(*) AS n FROM {T.cases} c WHERE {sql} GROUP BY c.decision, c.review_status", args):
        by_decision[r.decision] = by_decision.get(r.decision, 0) + r.n
        by_review[r.review_status] = by_review.get(r.review_status, 0) + r.n
        total += r.n
    tests: dict[str, dict] = {p["key"]: {} for p in parameters.PARAMETERS}
    for r in db.all(f"SELECT p.parameter AS k, p.status AS st, COUNT(*) AS n FROM {T.case_params} p JOIN {T.cases} c ON c.id = p.case_id "
                    f"WHERE {sql} GROUP BY p.parameter, p.status", args):
        tests.setdefault(r.k, {})[r.st] = r.n
    return {"total": total, "decisions": {d: by_decision.get(d, 0) for d in DECISIONS},
            "review": {REVIEW_STATES[k]: by_review.get(k, 0) for k in REVIEW_STATES},
            "tests": [{"key": p["key"], "label": p["label"], "results": [{"status": s, "tone": t, "count": tests[p["key"]].get(s, 0)} for s, t in p["outcomes"]]}
                      for p in parameters.PARAMETERS]}


def catalogue() -> dict:
    groups: dict[str, list[dict]] = {}
    for k, (label, group, _) in COLUMNS.items():
        groups.setdefault(group, []).append({"key": k, "label": label})
    return {"decisions": list(DECISIONS), "review_states": [{"key": k, "label": v} for k, v in REVIEW_STATES.items()],
            "tests": [{"key": p["key"], "label": p["label"], "outcomes": [o[0] for o in p["outcomes"]]} for p in parameters.PARAMETERS],
            "column_groups": [{"group": g, "columns": c} for g, c in groups.items()], "default_columns": DEFAULT_COLUMNS,
            "pdf": {"row_cap": PDF_ROW_CAP, "max_columns": PDF_MAX_COLUMNS, "default_columns": PDF_DEFAULT}}


# ---- PDF ----------------------------------------------------------------------------------------------------------------
def build_pdf(db: Db, f: Filters, keys: list[str], who: str, overview: dict | None = None) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    s = summary(db, f)
    ss = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=ss["BodyText"], fontSize=7.5, leading=9)
    head = ParagraphStyle("head", parent=small, textColor=colors.white, fontName="Helvetica-Bold")
    muted = ParagraphStyle("muted", parent=ss["BodyText"], fontSize=9, textColor=colors.HexColor("#555555"))
    ink, line, band = colors.HexColor("#1f2937"), colors.HexColor("#d1d5db"), colors.HexColor("#f3f4f6")

    class Numbered(canvas.Canvas):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._pages = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            for st in self._pages:
                self.__dict__.update(st)
                self.setFont("Helvetica", 8)
                self.setFillColor(colors.HexColor("#6b7280"))
                self.drawRightString(landscape(A4)[0] - 12 * mm, 8 * mm, f"Page {self._pageNumber} of {len(self._pages)}")
                self.drawString(12 * mm, 8 * mm, "Video analysis report · confidential")
                super().showPage()
            super().save()

    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=14 * mm,
                            title="Video analysis report", author=who)
    width = landscape(A4)[0] - 24 * mm
    flow = [Paragraph("Video analysis report", ss["Title"]),
            Paragraph(f"Generated {datetime.now(timezone.utc):%d %b %Y %H:%M} UTC by {who}", muted),
            Paragraph("<b>Filters:</b> " + "; ".join(describe(f)), muted), Spacer(1, 6 * mm)]

    def box(data, widths, header=True):
        t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
        st = [("FONTSIZE", (0, 0), (-1, -1), 8.5), ("TEXTCOLOR", (0, 0), (-1, -1), ink), ("GRID", (0, 0), (-1, -1), 0.25, line),
              ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
        if header:
            st += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#374151")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                   ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, band])]
        t.setStyle(TableStyle(st))
        return t

    total = s["total"]
    pct = lambda n: f"{n / total:.0%}" if total else "–"  # noqa: E731
    flow.append(Paragraph(f"<b>{total:,}</b> videos match", ss["Heading3"]))
    if overview:
        from .casepdf import clean
        body = [Paragraph(f"<b>{clean(overview['headline'])}</b>", ParagraphStyle("ov", parent=ss["BodyText"], fontSize=11, leading=15)), Spacer(1, 3),
                Paragraph(clean(overview["summary"]), ss["BodyText"])]
        for title, key in (("Stands out", "highlights"), ("Worth following up", "watch_points")):
            if overview.get(key):
                body += [Spacer(1, 4), Paragraph(f"<b>{title}</b>", ss["BodyText"])] + [Paragraph("- " + clean(x), ss["BodyText"]) for x in overview[key]]
        body += [Spacer(1, 4), Paragraph("Written by an AI assistant from the counts in this report; it explains them and is not a further check.", muted)]
        ot = Table([[body]], colWidths=[width])
        ot.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#eef4fb")), ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#c9dcf2")),
                                ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
        flow += [ot, Spacer(1, 5 * mm)]
    if total:
        flow.append(box([["Automatic decision", "Videos", "Share"]] + [[d, f"{n:,}", pct(n)] for d, n in s["decisions"].items()],
                        [70 * mm, 30 * mm, 25 * mm]))
        flow.append(Spacer(1, 4 * mm))
        flow.append(box([["Review", "Videos", "Share"]] + [[k, f"{n:,}", pct(n)] for k, n in s["review"].items()], [70 * mm, 30 * mm, 25 * mm]))
        flow += [Spacer(1, 5 * mm), Paragraph("Results by test", ss["Heading3"])]
        trows = [["Test"] + ["Result", "Videos", "Share"]]
        for t in s["tests"]:
            for i, r in enumerate(t["results"]):
                trows.append([t["label"] if i == 0 else "", r["status"], f"{r['count']:,}", pct(r["count"])])
        flow.append(box(trows, [60 * mm, 55 * mm, 30 * mm, 25 * mm]))
        shown = min(total, PDF_ROW_CAP)
        flow += [Spacer(1, 6 * mm), Paragraph(f"Videos ({shown:,} of {total:,}, newest first)", ss["Heading3"])]
        if total > PDF_ROW_CAP:
            flow.append(Paragraph(f"The list is limited to {PDF_ROW_CAP:,} videos in a PDF. Download the CSV for all {total:,}.", muted))
        data = [[Paragraph(COLUMNS[k][0], head) for k in keys]]
        for r in rows(db, f, keys, PDF_ROW_CAP):
            data.append([Paragraph(str(_cell(v)).replace("&", "&amp;").replace("<", "&lt;"), small) for v in r])
        t = Table(data, colWidths=[width / len(keys)] * len(keys), repeatRows=1)
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#374151")), ("GRID", (0, 0), (-1, -1), 0.25, line),
                               ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, band]), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        flow.append(t)
    else:
        flow.append(Paragraph("No videos match these filters.", muted))
    doc.build(flow, canvasmaker=Numbered)
    return out.getvalue()
