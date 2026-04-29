"""
sve_html_report.py
------------------
Standalone HTML report generator for SVE Summary data.

Usage:
    from sve_html_report import generate_html_report

    generate_html_report(
        output_data=output_data,
        title="SVE Results Summary",
        subtitle="Matter 1.6",
        filename="sve_summary_report.html"
    )
"""

import datetime

# Section header constants — must match the main script
SECTION_NOT_EXECUTED = "---- Not Executed Yet----"
SECTION_LOW_PASS     = "---- Pass Count < Required ----"
SECTION_PASSED       = "---- Passed Rule of Three ----"


def generate_html_report(
    output_data,
    title="SVE Results Summary",
    subtitle="Matter 1.6",
    filename="sve_summary_report.html"
):
    """
    Generate an interactive HTML report from SVE summary output_data.

    Args:
        output_data : list — same list written to the Google Sheets Summary tab.
                      First row must be the header row.
        title       : str  — main heading shown in the top bar and footer.
        subtitle    : str  — secondary heading (e.g. spec version).
        filename    : str  — output file path.
    """
    if not output_data or len(output_data) < 2:
        print("⚠️  generate_html_report: output_data is empty, skipping HTML generation.")
        return

    header = output_data[0]
    rows   = output_data[1:]

    # ── Parse sections ─────────────────────────────────────────────────────
    sections = {
        "not_executed": {"label": "Not Executed Yet",         "emoji": "&#10060;", "rows": []},
        "low_pass":     {"label": "Pass Count &lt; Required", "emoji": "&#9888;",  "rows": []},
        "passed":       {"label": "Passed Rule of Three",     "emoji": "&#9989;",  "rows": []},
    }
    section_map = {
        SECTION_NOT_EXECUTED: "not_executed",
        SECTION_LOW_PASS:     "low_pass",
        SECTION_PASSED:       "passed",
    }
    current = None
    for row in rows:
        if not row or not row[0]:
            continue
        key = section_map.get(row[0].strip())
        if key:
            current = key
            continue
        if current and len(row) >= len(header):
            sections[current]["rows"].append(row)

    # ── Quick stats ────────────────────────────────────────────────────────
    total    = sum(len(s["rows"]) for s in sections.values())
    n_passed = len(sections["passed"]["rows"])
    n_low    = len(sections["low_pass"]["rows"])
    n_never  = len(sections["not_executed"]["rows"])
    n_cert   = 0
    if "Certification Status" in header:
        cert_idx = header.index("Certification Status")
        for s in sections.values():
            for r in s["rows"]:
                if len(r) > cert_idx and r[cert_idx].strip() == "Certifiable":
                    n_cert += 1

    ts = datetime.datetime.now().strftime("%d %b %Y, %H:%M")

    # ── Column indices ─────────────────────────────────────────────────────
    def ci(name):
        try:    return header.index(name)
        except: return None

    ci_tc    = ci("Test Case Name")
    ci_pass  = ci("Pass Count")
    ci_th    = ci("Can TH run be counted?")
    ci_fail  = ci("Fail Count")
    ci_nt    = ci("Not Tested Count")
    ci_runs  = ci("Number of runs required")
    ci_final = ci("Final # runs required")
    ci_cert  = ci("Certification Status")
    ci_nl    = ci("New/Legacy")
    ci_cmt   = ci("Comments")

    # ── HTML helpers ───────────────────────────────────────────────────────
    def g(row, idx):
        if idx is None or idx >= len(row):
            return ""
        return str(row[idx]).strip()

    def cert_badge(status):
        s = status.strip()
        cls = {
            "Certifiable":               "badge-cert",
            "Provisional":               "badge-prov",
            "New Changes - Provisional": "badge-new",
        }.get(s, "badge-none")
        return '<span class="badge ' + cls + '">' + s + '</span>'

    def runs_pill(val):
        try:
            v = int(val)
            cls = {0: "pill-0", 1: "pill-1", 2: "pill-2", 3: "pill-3"}.get(v, "pill-high")
            return '<span class="pill ' + cls + '">' + str(v) + '</span>'
        except Exception:
            return str(val)

    def nl_tag(val):
        if not val or not val.strip():
            return ""
        cls = "tag-new" if "new" in val.strip().lower() else "tag-legacy"
        return '<span class="tag ' + cls + '">' + val.strip() + '</span>'

    # ── Table rows ─────────────────────────────────────────────────────────
    PLACEHOLDERS = (
        "All test cases executed at least once",
        "No test cases below required pass count",
        "No remaining test cases",
    )

    def build_rows(section_rows):
        parts = []
        for row in section_rows:
            tc = g(row, ci_tc)
            if tc in PLACEHOLDERS:
                parts.append(
                    '<tr class="placeholder-row"><td colspan="10">' + tc + '</td></tr>'
                )
                continue
            th_display = g(row, ci_th)
            if th_display == "0":
                th_display = ""
            parts.append(
                "<tr>"
                + '<td class="tc-name">'    + tc                                        + "</td>"
                + '<td class="num">'         + g(row, ci_pass)                           + "</td>"
                + '<td class="num th-col">'  + th_display                                + "</td>"
                + '<td class="num">'         + g(row, ci_fail)                           + "</td>"
                + '<td class="num muted">'   + g(row, ci_nt)                             + "</td>"
                + '<td class="num">'         + g(row, ci_runs)                           + "</td>"
                + '<td class="num">'         + runs_pill(g(row, ci_final))               + "</td>"
                + "<td>"                     + (cert_badge(g(row, ci_cert)) if g(row, ci_cert) else "") + "</td>"
                + "<td>"                     + nl_tag(g(row, ci_nl))                     + "</td>"
                + '<td class="comment-col">' + g(row, ci_cmt)                           + "</td>"
                + "</tr>"
            )
        return "\n".join(parts)

    # ── Section accordion ──────────────────────────────────────────────────
    def build_section(key, open_=True):
        s         = sections[key]
        cnt       = len(s["rows"])
        open_attr = " open" if open_ else ""
        plural    = "s" if cnt != 1 else ""
        out  = '<details class="section-block"' + open_attr + ">\n"
        out += '<summary class="section-header sec-' + key + '">\n'
        out += '<span class="sec-emoji">' + s["emoji"] + "</span>\n"
        out += '<span class="sec-label">' + s["label"] + "</span>\n"
        out += '<span class="sec-count">' + str(cnt) + " test case" + plural + "</span>\n"
        out += "</summary>\n"
        out += '<div class="table-wrap">\n'
        out += '<table class="data-table">\n'
        out += "<thead><tr>"
        for th in ["Test Case", "Pass", "TH", "Fail", "Not Tested",
                   "Runs Req", "Final Runs", "Status", "Type", "Comments"]:
            extra = ' class="tc-name-h"' if th == "Test Case" else (
                    ' class="th-col"'    if th == "TH"          else "")
            out += "<th" + extra + ">" + th + "</th>"
        out += "</tr></thead>\n"
        out += "<tbody>\n" + build_rows(s["rows"]) + "\n</tbody>\n"
        out += "</table></div></details>\n"
        return out

    # ── CSS ────────────────────────────────────────────────────────────────
    css = "\n".join([
        "*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }",
        ":root {",
        "    --bg: #f4f5f7; --surface: #ffffff; --border: #e2e5ea;",
        "    --text: #1a1d23; --muted: #6b7280; --accent: #2563eb; --accent-soft: #eff4ff;",
        "    --cert-bg: #dcfce7; --cert-fg: #15803d;",
        "    --prov-bg: #fff3cd; --prov-fg: #92400e;",
        "    --newp-bg: #fef9c3; --newp-fg: #713f12;",
        "    --pill0-bg: #bbf7d0; --pill0-fg: #14532d;",
        "    --pill1-bg: #d1fae5; --pill1-fg: #065f46;",
        "    --pill2-bg: #fef08a; --pill2-fg: #713f12;",
        "    --pill3-bg: #fed7aa; --pill3-fg: #7c2d12;",
        "    --pill-hi-bg: #fecaca; --pill-hi-fg: #7f1d1d;",
        "    --sec-ne-bg: #fff1f2; --sec-ne-bd: #fda4af;",
        "    --sec-lp-bg: #fffbeb; --sec-lp-bd: #fcd34d;",
        "    --sec-ps-bg: #f0fdf4; --sec-ps-bd: #86efac;",
        "    --radius: 8px;",
        "    --shadow: 0 1px 3px rgba(0,0,0,.08);",
        "    --shadow-md: 0 4px 12px rgba(0,0,0,.1);",
        "}",
        "body { font-family: 'IBM Plex Sans', sans-serif; background: var(--bg); color: var(--text); font-size: 14px; line-height: 1.5; }",
        ".topbar { background: var(--text); color: #fff; padding: 0 32px; height: 52px; display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; z-index: 100; box-shadow: var(--shadow-md); }",
        ".topbar-title { font-family: 'IBM Plex Mono', monospace; font-size: 13px; font-weight: 600; color: #fff; }",
        ".topbar-title span { color: #93c5fd; }",
        ".topbar-ts { font-size: 11px; color: #9ca3af; font-family: 'IBM Plex Mono', monospace; }",
        ".page { max-width: 1400px; margin: 0 auto; padding: 28px 24px 60px; }",
        ".stats-row { display: flex; gap: 14px; margin-bottom: 28px; flex-wrap: wrap; }",
        ".stat-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px 22px; flex: 1; min-width: 160px; box-shadow: var(--shadow); display: flex; flex-direction: column; gap: 4px; }",
        ".stat-card .val { font-family: 'IBM Plex Mono', monospace; font-size: 28px; font-weight: 600; line-height: 1; }",
        ".stat-card .lbl { font-size: 11px; text-transform: uppercase; letter-spacing: .8px; color: var(--muted); font-weight: 500; }",
        ".stat-card.s-total .val { color: var(--accent); }",
        ".stat-card.s-cert  .val { color: #15803d; }",
        ".stat-card.s-prov  .val { color: #b45309; }",
        ".stat-card.s-never .val { color: #dc2626; }",
        ".controls { display: flex; gap: 12px; margin-bottom: 20px; align-items: center; flex-wrap: wrap; }",
        ".search-wrap { position: relative; flex: 1; min-width: 220px; }",
        ".search-wrap svg { position: absolute; left: 12px; top: 50%; transform: translateY(-50%); color: var(--muted); }",
        "#search { width: 100%; padding: 9px 12px 9px 36px; border: 1px solid var(--border); border-radius: var(--radius); font-family: 'IBM Plex Sans', sans-serif; font-size: 13px; background: var(--surface); outline: none; transition: border-color .15s; }",
        "#search:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(37,99,235,.1); }",
        ".btn { padding: 8px 16px; border-radius: var(--radius); border: 1px solid var(--border); background: var(--surface); font-family: 'IBM Plex Sans', sans-serif; font-size: 12px; font-weight: 500; cursor: pointer; transition: all .15s; color: var(--text); white-space: nowrap; }",
        ".btn:hover { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); }",
        ".btn-export { background: var(--accent); color: #fff; border-color: var(--accent); }",
        ".btn-export:hover { background: #1d4ed8; color: #fff; }",
        ".section-block { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); margin-bottom: 16px; box-shadow: var(--shadow); overflow: hidden; }",
        ".section-header { display: flex; align-items: center; gap: 10px; padding: 14px 20px; cursor: pointer; user-select: none; list-style: none; transition: background .15s; }",
        ".section-header::-webkit-details-marker { display: none; }",
        ".section-header::before { content: '\\25B6'; font-size: 10px; color: var(--muted); transition: transform .2s; margin-right: 2px; }",
        "details[open] .section-header::before { transform: rotate(90deg); }",
        ".sec-not_executed { background: var(--sec-ne-bg); border-bottom: 1px solid var(--sec-ne-bd); }",
        ".sec-low_pass     { background: var(--sec-lp-bg); border-bottom: 1px solid var(--sec-lp-bd); }",
        ".sec-passed       { background: var(--sec-ps-bg); border-bottom: 1px solid var(--sec-ps-bd); }",
        ".sec-emoji { font-size: 16px; }",
        ".sec-label { font-weight: 600; font-size: 13px; flex: 1; }",
        ".sec-count { font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--muted); background: rgba(0,0,0,.06); padding: 2px 8px; border-radius: 20px; }",
        ".table-wrap { overflow-x: auto; }",
        ".data-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }",
        ".data-table thead tr { background: #f8f9fb; border-bottom: 2px solid var(--border); }",
        ".data-table th { padding: 10px 12px; text-align: left; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .6px; color: var(--muted); white-space: nowrap; cursor: pointer; user-select: none; }",
        ".data-table th:hover { color: var(--accent); }",
        ".data-table th.sorted-asc::after  { content: ' \u2191'; color: var(--accent); }",
        ".data-table th.sorted-desc::after { content: ' \u2193'; color: var(--accent); }",
        ".data-table tbody tr { border-bottom: 1px solid var(--border); transition: background .1s; }",
        ".data-table tbody tr:hover { background: var(--accent-soft); }",
        ".data-table tbody tr:last-child { border-bottom: none; }",
        ".data-table td { padding: 9px 12px; vertical-align: middle; }",
        ".tc-name { font-family: 'IBM Plex Mono', monospace; font-size: 11.5px; max-width: 340px; word-break: break-word; }",
        ".tc-name-h { min-width: 260px; }",
        ".num { text-align: center; font-family: 'IBM Plex Mono', monospace; }",
        ".muted { color: var(--muted); }",
        ".th-col { color: #2563eb; font-weight: 600; }",
        ".comment-col { font-size: 11.5px; color: var(--muted); font-style: italic; max-width: 200px; }",
        ".placeholder-row td { text-align: center; padding: 16px; color: var(--muted); font-style: italic; font-size: 12px; }",
        ".badge { display: inline-block; padding: 3px 9px; border-radius: 20px; font-size: 11px; font-weight: 600; white-space: nowrap; }",
        ".badge-cert { background: var(--cert-bg); color: var(--cert-fg); }",
        ".badge-prov { background: var(--prov-bg); color: var(--prov-fg); }",
        ".badge-new  { background: var(--newp-bg); color: var(--newp-fg); }",
        ".badge-none { background: #f1f5f9; color: var(--muted); }",
        ".pill { display: inline-block; padding: 2px 10px; border-radius: 20px; font-family: 'IBM Plex Mono', monospace; font-size: 11px; font-weight: 600; }",
        ".pill-0    { background: var(--pill0-bg); color: var(--pill0-fg); }",
        ".pill-1    { background: var(--pill1-bg); color: var(--pill1-fg); }",
        ".pill-2    { background: var(--pill2-bg); color: var(--pill2-fg); }",
        ".pill-3    { background: var(--pill3-bg); color: var(--pill3-fg); }",
        ".pill-high { background: var(--pill-hi-bg); color: var(--pill-hi-fg); }",
        ".tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: .5px; }",
        ".tag-new    { background: #dbeafe; color: #1e40af; }",
        ".tag-legacy { background: #f3e8ff; color: #6b21a8; }",
        "tr.hidden { display: none; }",
        ".footer { text-align: center; padding: 32px 0 0; font-size: 11px; color: var(--muted); font-family: 'IBM Plex Mono', monospace; }",
        ".legend { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow); margin-bottom: 16px; overflow: hidden; }",
        ".legend-header { padding: 14px 20px; font-weight: 600; font-size: 13px; background: #f8f9fb; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 8px; }",
        ".legend-header span { font-size: 15px; }",
        ".legend-body { display: grid; grid-template-columns: 1fr 1fr; gap: 0; }",
        "@media (max-width: 768px) { .legend-body { grid-template-columns: 1fr; } }",
        ".legend-col { padding: 20px 24px; }",
        ".legend-col:first-child { border-right: 1px solid var(--border); }",
        ".legend-col h4 { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .8px; color: var(--muted); margin-bottom: 14px; }",
        ".legend-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }",
        ".legend-table tr { border-bottom: 1px solid var(--border); }",
        ".legend-table tr:last-child { border-bottom: none; }",
        ".legend-table td { padding: 8px 10px; vertical-align: top; line-height: 1.5; }",
        ".legend-table td:first-child { white-space: nowrap; font-weight: 600; font-family: 'IBM Plex Mono', monospace; font-size: 11.5px; color: var(--text); width: 110px; padding-right: 16px; }",
        ".legend-table td:last-child { color: var(--muted); }",
        ".legend-status { display: flex; flex-direction: column; gap: 10px; }",
        ".legend-status-item { display: flex; align-items: flex-start; gap: 10px; font-size: 12.5px; line-height: 1.5; }",
        ".legend-status-item .badge { flex-shrink: 0; margin-top: 1px; }",
        ".legend-status-item p { color: var(--muted); margin: 0; }",
    ])

    # ── JS ─────────────────────────────────────────────────────────────────
    js = "\n".join([
        "document.getElementById('search').addEventListener('input', function() {",
        "    var q = this.value.toLowerCase().trim();",
        "    document.querySelectorAll('.data-table tbody tr').forEach(function(tr) {",
        "        if (tr.classList.contains('placeholder-row')) return;",
        "        var tc = tr.querySelector('.tc-name');",
        "        if (!tc) return;",
        "        tr.classList.toggle('hidden', q !== '' && !tc.textContent.toLowerCase().includes(q));",
        "    });",
        "    if (q) {",
        "        document.querySelectorAll('.section-block').forEach(function(d) {",
        "            var vis = d.querySelectorAll('tbody tr:not(.placeholder-row):not(.hidden)').length > 0;",
        "            if (vis) d.setAttribute('open', '');",
        "        });",
        "    }",
        "});",
        "function expandAll() {",
        "    document.querySelectorAll('.section-block').forEach(function(d) { d.setAttribute('open', ''); });",
        "}",
        "function collapseAll() {",
        "    document.querySelectorAll('.section-block').forEach(function(d) { d.removeAttribute('open'); });",
        "}",
        "document.querySelectorAll('.data-table th').forEach(function(th, colIdx) {",
        "    var asc = true;",
        "    th.addEventListener('click', function() {",
        "        var table = th.closest('table');",
        "        var tbody = table.querySelector('tbody');",
        "        var rows  = Array.from(tbody.querySelectorAll('tr:not(.placeholder-row)'));",
        "        rows.sort(function(a, b) {",
        "            var av = a.cells[colIdx] ? a.cells[colIdx].textContent.trim() : '';",
        "            var bv = b.cells[colIdx] ? b.cells[colIdx].textContent.trim() : '';",
        "            var an = parseFloat(av), bn = parseFloat(bv);",
        "            if (!isNaN(an) && !isNaN(bn)) return asc ? an - bn : bn - an;",
        "            return asc ? av.localeCompare(bv) : bv.localeCompare(av);",
        "        });",
        "        rows.forEach(function(r) { tbody.appendChild(r); });",
        "        table.querySelectorAll('th').forEach(function(t) { t.classList.remove('sorted-asc', 'sorted-desc'); });",
        "        th.classList.add(asc ? 'sorted-asc' : 'sorted-desc');",
        "        asc = !asc;",
        "    });",
        "});",
        "function exportCSV() {",
        "    var cols = ['Test Case','Pass','TH','Fail','Not Tested','Runs Req','Final Runs','Status','Type','Comments'];",
        "    var rows = [cols.join(',')];",
        "    document.querySelectorAll('.data-table tbody tr:not(.placeholder-row):not(.hidden)').forEach(function(tr) {",
        "        var cells = Array.from(tr.querySelectorAll('td')).map(function(td) {",
        "            return '\"' + td.textContent.trim().replace(/\"/g, '\"\"') + '\"';",
        "        });",
        "        if (cells.length >= 10) rows.push(cells.join(','));",
        "    });",
        "    var blob = new Blob([rows.join('\\n')], {type: 'text/csv'});",
        "    var a = document.createElement('a');",
        "    a.href = URL.createObjectURL(blob);",
        "    a.download = 'sve_summary.csv';",
        "    a.click();",
        "}",
    ])

    # ── Assemble HTML ──────────────────────────────────────────────────────
    topbar_text = title + " &nbsp;/&nbsp; <span>" + subtitle + "</span>"
    footer_text = title + " &nbsp;&middot;&nbsp; " + subtitle + " &nbsp;&middot;&nbsp; " + ts

    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="UTF-8"/>',
        '<meta name="viewport" content="width=device-width, initial-scale=1.0"/>',
        "<title>" + title + " - " + subtitle + "</title>",
        '<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap" rel="stylesheet"/>',
        "<style>", css, "</style>",
        "</head>",
        "<body>",
        '<div class="topbar">',
        '  <div class="topbar-title">' + topbar_text + "</div>",
        '  <div class="topbar-ts">Generated: ' + ts + "</div>",
        "</div>",
        '<div class="page">',
        '  <div class="stats-row">',
        '    <div class="stat-card s-total"><div class="val">' + str(total)    + '</div><div class="lbl">Total Test Cases</div></div>',
        '    <div class="stat-card s-cert"><div class="val">'  + str(n_cert)   + '</div><div class="lbl">Certifiable</div></div>',
        '    <div class="stat-card s-prov"><div class="val">'  + str(n_low)    + '</div><div class="lbl">Pass Count &lt; Required</div></div>',
        '    <div class="stat-card s-never"><div class="val">' + str(n_never)  + '</div><div class="lbl">Not Executed Yet</div></div>',
        '    <div class="stat-card"><div class="val" style="color:#6b21a8">' + str(n_passed) + '</div><div class="lbl">Passed Rule of Three</div></div>',
        "  </div>",
        '  <div class="controls">',
        '    <div class="search-wrap">',
        '      <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>',
        '      <input id="search" type="text" placeholder="Search test case name..."/>',
        "    </div>",
        '    <button class="btn" onclick="expandAll()">Expand All</button>',
        '    <button class="btn" onclick="collapseAll()">Collapse All</button>',
        '    <button class="btn btn-export" onclick="exportCSV()">Export CSV</button>',
        "  </div>",
        build_section("not_executed", open_=True),
        build_section("low_pass",     open_=True),
        build_section("passed",       open_=False),
        '<div class="legend">',
        '  <div class="legend-header"><span>&#9432;</span> How to read this report</div>',
        '  <div class="legend-body">',
        '    <div class="legend-col">',
        '      <h4>Column Descriptions</h4>',
        '      <table class="legend-table">',
        '        <tr><td>Pass</td><td>Number of unique companies that successfully passed this test case with at least one device</td></tr>',
        '        <tr><td>TH</td><td>An additional pass credited from a Test Harness run (1 = counted, 0 or blank = not counted)</td></tr>',
        '        <tr><td>Fail</td><td>Number of unique companies where at least one device failed this test case</td></tr>',
        '        <tr><td>Not Tested</td><td>Number of results reported as Not Tested — test was not executed for those entries</td></tr>',
        '        <tr><td>Runs Req</td><td>How many passing companies are needed for certification. Reduced if passes were already recorded in a previous SVE event</td></tr>',
        '        <tr><td>Final Runs</td><td>How many more passing companies are still needed to reach the required count</td></tr>',
        '        <tr><td>Status</td><td>Current certification readiness of the test case — see descriptions on the right</td></tr>',
        '        <tr><td>Type</td><td>Whether this test case is newly introduced (New) or existed in a prior release (Legacy)</td></tr>',
        '        <tr><td>Comments</td><td>Additional context — for example, if passes carried over from a previous SVE event</td></tr>',
        '      </table>',
        '    </div>',
        '    <div class="legend-col">',
        '      <h4>Certification Status</h4>',
        '      <div class="legend-status">',
        '        <div class="legend-status-item"><span class="badge badge-cert">Certifiable</span><p>This test case has received enough passing results (including any TH credit and previous SVE runs) to meet certification requirements</p></div>',
        '        <div class="legend-status-item"><span class="badge badge-prov">Provisional</span><p>Some companies have passed but the required number has not been reached yet</p></div>',
        '        <div class="legend-status-item"><span class="badge badge-new">New Changes - Provisional</span><p>This is a newly introduced change in a legacy test case that requires at least one pass — none received yet</p></div>',
        '      </div>',
        '    </div>',
        '  </div>',
        '</div>',
        '  <div class="footer">' + footer_text + "</div>",
        "</div>",
        "<script>", js, "</script>",
        "</body>",
        "</html>",
    ]

    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))

    print("✅ HTML report generated: " + filename)
