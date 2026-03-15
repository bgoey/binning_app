"""
binning/export.py — Code generation and CSV export.

Completely independent of Flask and the binning algorithm.
Takes a results dict (already computed) and produces output artefacts.
"""


def generate_python_code(binning_results: dict, outcome_col: str) -> str:
    """
    Generate a self-contained woe_transformer.py from binning results.
    """
    lines = [
        '"""',
        "Auto-generated WoE binning transformer.",
        "Apply to any pandas DataFrame with the same columns.",
        '"""',
        "",
        "import numpy as np",
        "import pandas as pd",
        "",
        "",
        "# ── Bin definitions ─────────────────────────────────────────────",
        "",
        "NUMERIC_BINS = {",
    ]

    for col, info in binning_results.items():
        if info["type"] != "numeric":
            continue
        cuts      = info.get("cuts", [])
        bins_list = info["bins"]
        woe_map   = []
        for i, b in enumerate(bins_list):
            low  = float("-inf") if i == 0 \
                   else (cuts[i-1] if i-1 < len(cuts) else float("inf"))
            high = cuts[i] if i < len(cuts) else float("inf")
            woe_map.append((low, high, b["woe"]))
        lines.append(f"    {col!r}: {woe_map!r},")

    lines += ["}", "", "CATEGORICAL_BINS = {"]

    for col, info in binning_results.items():
        if info["type"] != "categorical":
            continue
        woe_map = {b["label"]: b["woe"] for b in info["bins"]}
        lines.append(f"    {col!r}: {woe_map!r},")

    lines += [
        "}",
        "",
        "",
        "def apply_woe(df: pd.DataFrame) -> pd.DataFrame:",
        '    """',
        "    Apply WoE encoding to a copy of df.",
        "    Returns the transformed DataFrame with _woe suffix columns.",
        '    """',
        "    out = df.copy()",
        "",
        "    for col, bins in NUMERIC_BINS.items():",
        "        if col not in out.columns:",
        "            continue",
        "        woe_col = col + '_woe'",
        "        out[woe_col] = np.nan",
        "        for (low, high, woe) in bins:",
        "            mask = (out[col] > low) & (out[col] <= high)",
        "            out.loc[mask, woe_col] = woe",
        "",
        "    for col, woe_map in CATEGORICAL_BINS.items():",
        "        if col not in out.columns:",
        "            continue",
        "        out[col + '_woe'] = out[col].map(woe_map)",
        "",
        "    return out",
        "",
        "",
        "def get_bin_label_numeric(col: str, value: float) -> str:",
        '    """Return the bin label for a numeric value."""',
        "    bins = NUMERIC_BINS.get(col, [])",
        "    for (low, high, woe) in bins:",
        "        if low < value <= high:",
        "            lo_str = '-inf' if low == float('-inf') else str(low)",
        "            hi_str = 'inf'  if high == float('inf') else str(high)",
        "            return f'({lo_str}, {hi_str}]'",
        "    return 'out_of_range'",
        "",
        "",
        "def score_dataframe(df: pd.DataFrame) -> pd.DataFrame:",
        '    """Apply WoE and compute a raw scorecard sum per row."""',
        "    woe_df   = apply_woe(df)",
        "    woe_cols = [c for c in woe_df.columns if c.endswith('_woe')]",
        "    woe_df['score'] = woe_df[woe_cols].sum(axis=1)",
        "    return woe_df",
        "",
        "",
        "if __name__ == '__main__':",
        "    # Quick smoke test",
        f"    sample = pd.DataFrame({{c: [None] for c in {list(binning_results.keys())!r}}})",
        "    result = apply_woe(sample)",
        "    print(result.head())",
    ]

    return "\n".join(lines)


def generate_woe_csv(binning_results: dict) -> str:
    """
    Flat CSV with all bins, WoE, and IV across all features.
    Returns CSV as a string.
    """
    import io
    import csv

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "column", "type", "bin_label",
        "n", "events", "non_events", "event_rate",
        "woe", "iv", "total_iv",
    ])

    for col, info in binning_results.items():
        col_type = info["type"]
        total_iv = info.get("total_iv", "")
        for b in info["bins"]:
            if col_type == "numeric":
                label = f"({b.get('min', '-inf')}, {b.get('max', 'inf')}]"
            else:
                label = b.get("label", "")
            writer.writerow([
                col, col_type, label,
                b.get("n", ""),
                b.get("events", ""),
                b.get("non_events", ""),
                b.get("event_rate", ""),
                b.get("woe", ""),
                b.get("iv", ""),
                total_iv,
            ])

    return buf.getvalue()




def generate_woe_excel(binning_results: dict, outcome_col: str = "target") -> bytes:
    """
    Build a polished Excel workbook using xlsxwriter.
    One sheet per column: WoE table (left) + combo bar+line chart (right).
    Event rate line is on the secondary (right) Y axis.
    Returns raw .xlsx bytes.
    """
    import io
    import xlsxwriter

    buf = io.BytesIO()
    wb  = xlsxwriter.Workbook(buf, {"in_memory": True})

    # ── Formats ──────────────────────────────────────────────────────
    def fmt(**kw):
        defaults = {"font_name": "Arial", "font_size": 10, "border": 1,
                    "border_color": "#D9D9D9"}
        defaults.update(kw)
        return wb.add_format(defaults)

    F = {
        "title":    fmt(font_size=13, bold=True,  font_color="#1F3864", border=0),
        "subtitle": fmt(font_size=9,  italic=True, font_color="#666666", border=0),
        "hdr":      fmt(bold=True, bg_color="#1F3864", font_color="#FFFFFF",
                        align="center", valign="vcenter"),
        "lbl":      fmt(align="left",  valign="vcenter", bg_color="#FFFFFF"),
        "lbl_alt":  fmt(align="left",  valign="vcenter", bg_color="#F2F2F2"),
        "num":      fmt(align="right", valign="vcenter", bg_color="#FFFFFF"),
        "num_alt":  fmt(align="right", valign="vcenter", bg_color="#F2F2F2"),
        "pct":      fmt(align="right", valign="vcenter", bg_color="#FFFFFF",
                        num_format="0.00%"),
        "pct_alt":  fmt(align="right", valign="vcenter", bg_color="#F2F2F2",
                        num_format="0.00%"),
        "woe":      fmt(align="right", valign="vcenter", bg_color="#FFFFFF",
                        num_format="0.00000"),
        "woe_alt":  fmt(align="right", valign="vcenter", bg_color="#F2F2F2",
                        num_format="0.00000"),
        "tot":      fmt(bold=True, bg_color="#E2EFDA", align="right",
                        num_format="#,##0"),
        "tot_woe":  fmt(bold=True, bg_color="#E2EFDA", align="right",
                        num_format="0.00000"),
        "ctr":      fmt(align="center", valign="vcenter", bg_color="#FFFFFF"),
        "ctr_alt":  fmt(align="center", valign="vcenter", bg_color="#F2F2F2"),
    }

    def _iv_label(iv):
        if iv < 0.02: return "Useless"
        if iv < 0.1:  return "Weak"
        if iv < 0.3:  return "Medium"
        if iv < 0.5:  return "Strong"
        return "Suspicious"

    def _iv_bg(iv):
        if iv < 0.02: return "#D9D9D9"
        if iv < 0.1:  return "#FFD966"
        if iv < 0.3:  return "#70AD47"
        if iv < 0.5:  return "#4472C4"
        return "#FF0000"

    ranked = sorted(binning_results.items(),
                    key=lambda x: x[1].get("total_iv", 0), reverse=True)

    # ── Summary sheet ─────────────────────────────────────────────────
    ws = wb.add_worksheet("Summary")
    ws.hide_gridlines(2)
    ws.set_column(0, 0, 24); ws.set_column(1, 1, 13); ws.set_column(2, 2, 13)
    ws.set_column(3, 3, 8);  ws.set_column(4, 4, 12); ws.set_column(5, 5, 14)

    ws.set_row(0, 28); ws.merge_range("A1:F1", "WoE Binning Summary", F["title"])
    ws.set_row(1, 16)
    ws.merge_range("A2:F2", f"Outcome variable: {outcome_col}", F["subtitle"])
    ws.set_row(3, 18)
    for ci, h in enumerate(["Feature","Type","Mode","Bins","Total IV","IV Strength"]):
        ws.write(3, ci, h, F["hdr"])

    ws.freeze_panes(4, 0)

    for ri, (col, info) in enumerate(ranked, 4):
        iv   = info.get("total_iv", 0)
        alt  = ri % 2 == 0
        bg   = "#F2F2F2" if alt else "#FFFFFF"
        lf   = fmt(align="left",   valign="vcenter", bg_color=bg)
        cf   = fmt(align="center", valign="vcenter", bg_color=bg)
        nf   = fmt(align="center", valign="vcenter", bg_color=bg, num_format="0.0000")
        ivf  = fmt(bold=True, align="center", valign="vcenter",
                   bg_color=_iv_bg(iv),
                   font_color="#FFFFFF" if iv >= 0.1 else "#000000")
        ws.set_row(ri, 16)
        ws.write(ri, 0, col,                       lf)
        ws.write(ri, 1, info["type"],               cf)
        ws.write(ri, 2, info.get("bin_mode",""),    cf)
        ws.write(ri, 3, len(info["bins"]),          cf)
        ws.write(ri, 4, iv,                         nf)
        ws.write(ri, 5, _iv_label(iv),              ivf)

    # ── One sheet per column ──────────────────────────────────────────
    for col, info in ranked:
        bins     = info["bins"]
        col_type = info["type"]
        is_num   = col_type == "numeric"
        total_iv = info.get("total_iv", 0)
        bin_mode = info.get("bin_mode", "")
        n_bins   = len(bins)
        sname    = col[:31]

        ws = wb.add_worksheet(sname)
        ws.hide_gridlines(2)
        ws.freeze_panes(4, 0)

        ws.set_column(0, 0, 4)   # #
        ws.set_column(1, 1, 26)  # Bin
        ws.set_column(2, 2, 10)  # N
        ws.set_column(3, 3, 10)  # Events
        ws.set_column(4, 4, 12)  # Non-Events
        ws.set_column(5, 5, 12)  # Event Rate
        ws.set_column(6, 6, 10)  # WoE
        ws.set_column(7, 7, 10)  # IV
        ws.set_column(8, 8, 10)  # Cum IV

        # Title rows
        ws.set_row(0, 24)
        ws.merge_range(0, 0, 0, 9, col, F["title"])
        ws.set_row(1, 14)
        ws.merge_range(1, 0, 1, 9,
            f"Type: {col_type}   |   Mode: {bin_mode}   |   "
            f"Total IV: {total_iv:.6f}   |   {_iv_label(total_iv)}",
            F["subtitle"])
        ws.set_row(2, 6)   # spacer

        # Header row (row index 3 = Excel row 4)
        ws.set_row(3, 18)
        for ci, h in enumerate(["#","Bin / Category","N","Events","Non-Events",
                                  "Event Rate","WoE","IV","Cum. IV"]):
            ws.write(3, ci, h, F["hdr"])

        # Data rows start at row index 4 = Excel row 5
        DATA_START = 4
        cum_iv = 0.0
        labels, counts, event_rates = [], [], []

        for ri, b in enumerate(bins):
            row = DATA_START + ri
            alt = ri % 2 == 0
            lf  = F["lbl_alt"]  if alt else F["lbl"]
            nf  = F["num_alt"]  if alt else F["num"]
            pf  = F["pct_alt"]  if alt else F["pct"]
            wf  = F["woe_alt"]  if alt else F["woe"]
            cf  = F["ctr_alt"]  if alt else F["ctr"]

            cum_iv += b.get("iv", 0)
            lo, hi = b.get("min",""), b.get("max","")
            label  = (f"[{lo:.4g}, {hi:.4g}]"
                      if is_num and isinstance(lo, float)
                      else b.get("label", str(lo)))

            labels.append(label)
            counts.append(b.get("n", 0))
            event_rates.append(b.get("event_rate", 0))

            ws.set_row(row, 15)
            ws.write(row, 0, ri + 1,             cf)
            ws.write(row, 1, label,               lf)
            ws.write(row, 2, b.get("n",0),        nf)
            ws.write(row, 3, b.get("events",0),   nf)
            ws.write(row, 4, b.get("non_events",0), nf)
            ws.write(row, 5, b.get("event_rate",0), pf)
            ws.write(row, 6, b.get("woe",0),      wf)
            ws.write(row, 7, b.get("iv",0),       wf)
            ws.write(row, 8, cum_iv,               wf)

        # Totals row
        last_data = DATA_START + n_bins   # 0-indexed row after last bin
        ws.set_row(last_data, 15)
        ws.write    (last_data, 0, "Total",  F["tot"])
        ws.write    (last_data, 1, "",       F["tot"])
        ws.write_formula(last_data, 2, f"=SUM(C{DATA_START+1}:C{last_data})", F["tot"])
        ws.write_formula(last_data, 3, f"=SUM(D{DATA_START+1}:D{last_data})", F["tot"])
        ws.write_formula(last_data, 4, f"=SUM(E{DATA_START+1}:E{last_data})", F["tot"])
        ws.write    (last_data, 5, "",       F["tot"])
        ws.write    (last_data, 6, "",       F["tot"])
        ws.write_formula(last_data, 7, f"=SUM(H{DATA_START+1}:H{last_data})", F["tot_woe"])
        ws.write    (last_data, 8, "",       F["tot"])

        # ── Combo chart — xlsxwriter handles secondary axis correctly ──
        bar = wb.add_chart({"type": "column"})
        bar.add_series({
            "name":       "Count",
            "categories": [sname, DATA_START, 1, DATA_START + n_bins - 1, 1],
            "values":     [sname, DATA_START, 2, DATA_START + n_bins - 1, 2],
            "fill":       {"color": "#4472C4"},
            "border":     {"color": "#4472C4"},
            "gap":        80,
        })

        line = wb.add_chart({"type": "line"})
        line.add_series({
            "name":       "Event Rate",
            "categories": [sname, DATA_START, 1, DATA_START + n_bins - 1, 1],
            "values":     [sname, DATA_START, 5, DATA_START + n_bins - 1, 5],
            "line":       {"color": "#FF0000", "width": 2.0},
            "marker":     {"type": "circle", "size": 5,
                           "fill":   {"color": "#FF0000"},
                           "border": {"color": "#FF0000"}},
            "y2_axis":    True,
        })

        # set_y_axis on bar (left), set_y2_axis on line (right) — xlsxwriter requirement
        bar.set_y_axis({"name": "Count", "major_gridlines": {"visible": True}})
        bar.set_x_axis({"text_axis": True})
        line.set_y2_axis({"name": "Event Rate %", "num_format": "0.0%"})

        bar.combine(line)
        bar.set_size({"width": 680, "height": 420})
        bar.set_chartarea({"border": {"color": "#D9D9D9"}, "fill": {"color": "#FFFFFF"}})
        bar.set_plotarea({"fill": {"color": "#FFFFFF"}})
        bar.set_legend({"position": "bottom"})

        # Place chart at column K (index 10), row 4 (index 3)
        ws.insert_chart(3, 10, bar, {"x_offset": 0, "y_offset": 0})

    wb.close()
    buf.seek(0)
    return buf.read()


def generate_apps_script(binning_results: dict) -> str:
    """
    Generate a Google Apps Script that fixes all combo charts in the workbook
    so the Event Rate line uses the RIGHT / secondary Y axis.

    Usage:
      1. Open the exported .xlsx in Google Sheets
      2. Extensions → Apps Script
      3. Paste this script and click Run
    """
    # Collect sheet names that have charts (all column sheets, not Summary)
    col_sheets = [col[:31] for col in binning_results.keys()]

    lines = [
        "/**",
        " * Fix WoE combo charts: move Event Rate series to secondary (right) Y axis.",
        " * Run once after opening the exported .xlsx in Google Sheets.",
        " * Extensions → Apps Script → paste → Run",
        " */",
        "function fixSecondaryAxis() {",
        "  var ss = SpreadsheetApp.getActiveSpreadsheet();",
        f"  var sheetNames = {col_sheets!r};",
        "",
        "  sheetNames.forEach(function(name) {",
        "    var sheet = ss.getSheetByName(name);",
        "    if (!sheet) { Logger.log('Sheet not found: ' + name); return; }",
        "",
        "    var charts = sheet.getCharts();",
        "    if (charts.length === 0) { Logger.log('No chart in: ' + name); return; }",
        "",
        "    var chart = charts[0];",
        "    var builder = chart.modify();",
        "",
        "    // Series 0 = Count bars  → LEFT axis",
        "    // Series 1 = Event Rate  → RIGHT axis",
        "    builder.setSeriesOptions(0, {targetAxisIndex: 0});",
        "    builder.setSeriesOptions(1, {targetAxisIndex: 1});",
        "",
        "    // Label the axes",
        "    builder.setOption('vAxes', {",
        "      0: {title: 'Count'},",
        "      1: {title: 'Event Rate %', format: '0.0%'}",
        "    });",
        "",
        "    sheet.updateChart(builder.build());",
        "    Logger.log('Fixed: ' + name);",
        "  });",
        "",
        "  SpreadsheetApp.getUi().alert('Done! All Event Rate lines moved to right axis.');",
        "}",
    ]
    return "\n".join(lines)

