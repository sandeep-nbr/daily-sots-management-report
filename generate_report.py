#!/usr/bin/env python3
"""Build the NBR SOTS Daily Management Report from saved Salesforce query results.

Usage:
    python3 generate_report.py YYYY-MM-DD            # prints the SOQL queries for that day
    python3 generate_report.py YYYY-MM-DD --build    # builds HTML, PDF and Graph sendMail payload

Query results are read from data/<date>/<name>.json (names as in queries.json).
Outputs go to output/<date>/.
"""
import base64
import datetime as dt
import html
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
QUERIES = json.loads((ROOT / "queries.json").read_text())

NAVY = "#1E2761"
SUBTOTAL_BG = "#E6F2F3"
GRAND_BG = "#FDF1DC"
NOT_SPECIFIED = "Not specified"
CONNECTED = {"Call Complete", "ANSWER"}
BUSY = {"Customer Busy"}
OTHER = ("Other owners", "-")


def day_bounds(day):
    """UTC start/end of the IST calendar day."""
    hh, mm = (int(x) for x in CONFIG["timezone_offset"].lstrip("+").split(":"))
    start = dt.datetime.combine(day, dt.time()) - dt.timedelta(hours=hh, minutes=mm)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), (start + dt.timedelta(days=1)).strftime(fmt)


def render_queries(day):
    start, end = day_bounds(day)
    return {
        name: q.replace("{START}", start).replace("{END}", end).replace("{DATE}", day.isoformat())
        for name, q in QUERIES.items()
        if not name.startswith("_")
    }


def load(day, name):
    path = ROOT / "data" / day.isoformat() / f"{name}.json"
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        if not data.get("done", True):
            raise SystemExit(f"{path}: query result is paginated (done=false); narrow the query")
        data = data.get("records", [])
    return data


def phone_key(number):
    digits = "".join(ch for ch in str(number or "") if ch.isdigit())
    return digits[-10:]


class Roster:
    def __init__(self):
        self.rows = []  # (user name, team name)
        self.by_id = {}
        self.by_phone = {}
        for team in CONFIG["teams"]:
            for u in team["users"]:
                key = (u["name"], team["name"])
                self.rows.append(key)
                self.by_id[u["id"]] = key
                if u.get("phone"):
                    self.by_phone[phone_key(u["phone"])] = key

    def owner(self, owner_id):
        return self.by_id.get(owner_id, OTHER)


def source_of(value):
    return value or NOT_SPECIFIED


# ---------- metrics ----------

def leads_tables(day):
    by_source = defaultdict(lambda: defaultdict(int))
    by_user = defaultdict(lambda: defaultdict(int))
    roster = Roster()
    for r in load(day, "leads"):
        src = source_of(r.get("Lead_Source__c"))
        sub = r.get("Lead_Sub_Source__c") or NOT_SPECIFIED
        by_source[src][sub] += r["c"]
        by_user[roster.owner(r.get("OwnerId"))][src] += r["c"]
    return by_source, by_user


def calls_table(day):
    roster = Roster()
    stats = defaultdict(lambda: {"total": 0, "connected": 0, "busy": 0, "not": 0, "minutes": 0.0})
    for name, number_field in (("calls_out", "Call_From__c"), ("calls_in", "Call_To__c")):
        for r in load(day, name):
            user = roster.by_phone.get(phone_key(r.get(number_field)), ("Other numbers", "-"))
            s = stats[user]
            s["total"] += r["c"]
            status = r.get("Status__c")
            if status in CONNECTED:
                s["connected"] += r["c"]
            elif status in BUSY:
                s["busy"] += r["c"]
            else:
                s["not"] += r["c"]
            s["minutes"] += r.get("m") or 0
    return stats


def followups_table(day):
    roster = Roster()
    called_leads = {r["Lead_custom__c"] for r in load(day, "lead_calls")}
    stats = defaultdict(lambda: {"scheduled": 0, "done": 0})
    for t in load(day, "followups"):
        s = stats[roster.owner(t.get("OwnerId"))]
        s["scheduled"] += 1
        if t.get("Status") == "Completed" or t.get("WhatId") in called_leads:
            s["done"] += 1
    return stats


def site_visit_tables(day):
    """Returns (done visits by user x source, scheduled/done/re-scheduled by user)."""
    roster = Roster()
    done_by_source = defaultdict(lambda: defaultdict(int))
    sched = defaultdict(lambda: {"scheduled": 0, "done": 0, "rescheduled": 0})
    leads_with_visit = set()
    for v in load(day, "site_visits"):
        user = roster.owner(v.get("OwnerId"))
        status = v.get("Status__c")
        if status == "Cancelled" or not v.get("Lead__c"):
            continue
        leads_with_visit.add(v.get("Lead__c"))
        sched[user]["scheduled"] += 1
        if status == "Done":
            sched[user]["done"] += 1
            src = source_of((v.get("Lead__r") or {}).get("Lead_Source__c"))
            done_by_source[user][src] += 1
        elif status == "Re-Scheduled":
            sched[user]["rescheduled"] += 1
    # Walk-ins marked visit-done on the lead itself, without a Site Visit record that day.
    for lead in load(day, "walkin_visits"):
        if lead["Id"] in leads_with_visit:
            continue
        user = roster.owner(lead.get("OwnerId"))
        sched[user]["scheduled"] += 1
        sched[user]["done"] += 1
        done_by_source[user][source_of(lead.get("Lead_Source__c"))] += 1
    return done_by_source, sched


# ---------- table model ----------
# A table is (headers, rows) where each row is (kind, cells); kind in {"row", "sub", "grand"}.

def source_order(sources):
    return sorted(sources, key=lambda s: (s == NOT_SPECIFIED, s.lower()))


def team_grouped(headers, values_for, extra_users, numeric_cols, fmt=None):
    """Rows per roster user grouped by team with subtotals, then non-roster rows and grand total."""
    roster = Roster()
    fmt = fmt or (lambda v: str(v))
    rows, grand = [], [0] * numeric_cols
    for team in CONFIG["teams"]:
        sub = [0] * numeric_cols
        for u in team["users"]:
            vals = values_for((u["name"], team["name"]))
            rows.append(("row", [u["name"], team["name"]] + [fmt(v) for v in vals]))
            sub = [a + b for a, b in zip(sub, vals)]
        rows.append(("sub", [f"{team['name']} total", ""] + [fmt(v) for v in sub]))
        grand = [a + b for a, b in zip(grand, sub)]
    for key in extra_users:
        if key in roster.rows:
            continue
        vals = values_for(key)
        if any(vals):
            rows.append(("row", list(key) + [fmt(v) for v in vals]))
            grand = [a + b for a, b in zip(grand, vals)]
    rows.append(("grand", ["Grand total", ""] + [fmt(v) for v in grand]))
    return headers, rows


def build_tables(day):
    tables = []

    by_source, by_user = leads_tables(day)
    rows, grand = [], 0
    for src in source_order(by_source):
        subs = by_source[src]
        for sub in sorted(subs, key=lambda k: (-subs[k], k)):
            rows.append(("row", [src, sub, str(subs[sub])]))
        total = sum(subs.values())
        rows.append(("sub", [f"{src} total", "", str(total)]))
        grand += total
    rows.append(("grand", ["Grand total", "", str(grand)]))
    tables.append(("Source & sub-source wise leads received", (["Source", "Sub-source", "Leads"], rows)))

    srcs = source_order(by_source) or [NOT_SPECIFIED]
    tables.append(("Leads received by user", team_grouped(
        ["User", "Team"] + srcs + ["Total"],
        lambda k: [by_user[k][s] for s in srcs] + [sum(by_user[k][s] for s in srcs)],
        by_user.keys(), len(srcs) + 1)))

    calls = calls_table(day)
    tables.append(("Calls made by user", team_grouped(
        ["User", "Team", "Total calls", "Connected", "Busy", "Not connected", "Talk time (min)"],
        lambda k: [calls[k]["total"], calls[k]["connected"], calls[k]["busy"], calls[k]["not"],
                   round(calls[k]["minutes"], 2)] if k in calls else [0, 0, 0, 0, 0.0],
        calls.keys(), 5,
        fmt=lambda v: f"{v:.1f}" if isinstance(v, float) else str(v))))

    done_by_source, sched = site_visit_tables(day)
    sv_srcs = source_order({s for u in done_by_source.values() for s in u}) or [NOT_SPECIFIED]
    tables.append(("Source & user wise site visits", team_grouped(
        ["User", "Team"] + sv_srcs + ["Total"],
        lambda k: [done_by_source[k][s] for s in sv_srcs] + [sum(done_by_source[k][s] for s in sv_srcs)],
        done_by_source.keys(), len(sv_srcs) + 1)))

    fu = followups_table(day)
    tables.append(("Follow-ups (for the day)", team_grouped(
        ["User", "Team", "Scheduled", "Done", "Missed"],
        lambda k: [fu[k]["scheduled"], fu[k]["done"], fu[k]["scheduled"] - fu[k]["done"]],
        fu.keys(), 3)))

    tables.append(("Site visits - scheduled, done & re-scheduled (for the day)", team_grouped(
        ["User", "Team", "Scheduled", "Done", "Re-scheduled"],
        lambda k: [sched[k]["scheduled"], sched[k]["done"], sched[k]["rescheduled"]],
        sched.keys(), 3)))
    return tables


# ---------- renderers ----------

def render_html(day, tables):
    h3 = f'<h3 style="font-family:Cambria,Georgia,serif;color:{NAVY}">'
    style = {"row": "", "sub": f' style="background:{SUBTOTAL_BG};font-weight:bold"',
             "grand": f' style="background:{GRAND_BG};font-weight:bold"'}
    out = [
        '<div style="font-family:Calibri,Arial,sans-serif;font-size:13px">',
        f'<h2 style="font-family:Cambria,Georgia,serif;color:{NAVY};margin:0">{html.escape(CONFIG["report_title"])}</h2>',
        f"<p>{day:%d %b %Y}</p>",
    ]
    for title, (headers, rows) in tables:
        out.append(f"{h3}{html.escape(title)}</h3>")
        out.append('<table border="1" cellpadding="4" style="border-collapse:collapse;border-color:#C9CED8">')
        out.append(f'<tr style="background:{NAVY};color:#fff">'
                   + "".join(f"<th>{html.escape(h)}</th>" for h in headers) + "</tr>")
        for kind, cells in rows:
            out.append(f"<tr{style[kind]}>" + "".join(f"<td>{html.escape(c)}</td>" for c in cells) + "</tr>")
        out.append("</table>")
    out.append("</div>")
    return "\n".join(out)


def render_pdf(day, tables, path):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    h1 = ParagraphStyle("h1", fontName="Times-Bold", fontSize=16, textColor=colors.HexColor(NAVY), spaceAfter=4)
    h3 = ParagraphStyle("h3", fontName="Times-Bold", fontSize=12, textColor=colors.HexColor(NAVY),
                        spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle("body", fontName="Helvetica", fontSize=9)
    story = [Paragraph(html.escape(CONFIG["report_title"]), h1), Paragraph(f"{day:%d %b %Y}", body)]
    for title, (headers, rows) in tables:
        data = [headers] + [cells for _, cells in rows]
        ts = [
            ("FONT", (0, 0), (-1, -1), "Helvetica", 8),
            ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(NAVY)),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C9CED8")),
            ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]
        for i, (kind, _) in enumerate(rows, start=1):
            if kind != "row":
                ts.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor(SUBTOTAL_BG if kind == "sub" else GRAND_BG)))
                ts.append(("FONT", (0, i), (-1, i), "Helvetica-Bold", 8))
        table = Table(data, repeatRows=1, hAlign="LEFT")
        table.setStyle(TableStyle(ts))
        story.append(KeepTogether([Paragraph(html.escape(title), h3), table]))
    story.append(Spacer(1, 6 * mm))
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                      topMargin=15 * mm, bottomMargin=15 * mm,
                      title=f"{CONFIG['report_title']} - {day:%d %b %Y}", pageCompression=1).build(story)


def build(day):
    tables = build_tables(day)
    out_dir = ROOT / "output" / day.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    body = render_html(day, tables)
    (out_dir / "report.html").write_text(body)
    pdf_name = f"NBR-SOTS-Daily-Management-Report_{day.isoformat()}.pdf"
    pdf_path = out_dir / pdf_name
    render_pdf(day, tables, pdf_path)
    payload = {
        "message": {
            "subject": f"{CONFIG['report_title']} - {day:%d %b %Y}",
            "body": {"contentType": "HTML", "content": body},
            "toRecipients": [{"emailAddress": {"address": a}} for a in CONFIG["recipients"]],
            "attachments": [{
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": pdf_name,
                "contentType": "application/pdf",
                "contentBytes": base64.b64encode(pdf_path.read_bytes()).decode(),
            }],
        },
        "saveToSentItems": True,
    }
    (out_dir / "sendmail_payload.json").write_text(json.dumps(payload))
    grand = {title: rows[-1][1][-1] for title, (_, rows) in tables}
    print(json.dumps({"output_dir": str(out_dir), "pdf": pdf_name, "grand_totals": grand}, indent=2))


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    day = dt.date.fromisoformat(sys.argv[1])
    if "--build" in sys.argv:
        build(day)
    else:
        print(json.dumps(render_queries(day), indent=2))


if __name__ == "__main__":
    main()
