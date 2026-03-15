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
    Build a polished Excel workbook with a true combo bar+line chart per column.
    Uses raw XML injection because openpyxl's bar += line operator is broken.
    Returns raw .xlsx bytes.
    """
    import io, zipfile
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.drawing.spreadsheet_drawing import (
        SpreadsheetDrawing, OneCellAnchor, AnchorMarker
    )
    from openpyxl.drawing.xdr import XDRPositiveSize2D
    from openpyxl.drawing.spreadsheet_drawing import AnchorClientData

    # ── Palette ──────────────────────────────────────────────────────
    WHITE       = "FFFFFFFF"
    HEADER_FILL = "FF1F3864"
    ALT_FILL    = "FFF2F2F2"

    def _side(): return Side(style="thin", color="FFD9D9D9")
    def _border(): return Border(left=_side(), right=_side(), top=_side(), bottom=_side())
    def _hdr(): return PatternFill("solid", fgColor=HEADER_FILL)
    def _alt(): return PatternFill("solid", fgColor=ALT_FILL)
    def _wht(): return PatternFill("solid", fgColor=WHITE)

    def sc(cell, bold=False, fill=None, fc="FF000000", align="left", fmt=None):
        cell.font      = Font(name="Arial", bold=bold, color=fc, size=10)
        cell.alignment = Alignment(horizontal=align, vertical="center")
        if fill: cell.fill = fill
        if fmt:  cell.number_format = fmt
        cell.border = _border()

    def _iv_label(iv):
        if iv < 0.02: return "Useless"
        if iv < 0.1:  return "Weak"
        if iv < 0.3:  return "Medium"
        if iv < 0.5:  return "Strong"
        return "Suspicious"

    def _iv_color(iv):
        if iv < 0.02: return "FFD9D9D9"
        if iv < 0.1:  return "FFFFD966"
        if iv < 0.3:  return "FF70AD47"
        if iv < 0.5:  return "FF4472C4"
        return "FFFF0000"

    # ── Build workbook (data + layout only, no charts yet) ────────────
    wb  = Workbook()
    wb.remove(wb.active)

    # Track per-sheet chart metadata for XML injection later
    # { sheet_name: { n_bins, count_col, er_col, data_start_row } }
    chart_meta = {}

    # ── Summary sheet ─────────────────────────────────────────────────
    ws_sum = wb.create_sheet("Summary")
    ws_sum.sheet_view.showGridLines = False

    ws_sum.merge_cells("A1:F1")
    c = ws_sum["A1"]
    c.value     = "WoE Binning Summary"
    c.font      = Font(name="Arial", bold=True, size=14, color="FF1F3864")
    c.alignment = Alignment(horizontal="left", vertical="center")
    c.fill      = _wht()
    ws_sum.row_dimensions[1].height = 28

    ws_sum.merge_cells("A2:F2")
    c = ws_sum["A2"]
    c.value = f"Outcome variable: {outcome_col}"
    c.font  = Font(name="Arial", size=10, color="FF666666", italic=True)
    c.fill  = _wht()
    ws_sum.row_dimensions[2].height = 16

    hdrs = ["Feature", "Type", "Mode", "Bins", "Total IV", "IV Strength"]
    for ci, h in enumerate(hdrs, 1):
        c = ws_sum.cell(row=4, column=ci, value=h)
        sc(c, bold=True, fill=_hdr(), fc="FFFFFFFF", align="center")
    ws_sum.row_dimensions[4].height = 18

    ranked = sorted(binning_results.items(),
                    key=lambda x: x[1].get("total_iv", 0), reverse=True)

    for ri, (col, info) in enumerate(ranked, 5):
        iv  = info.get("total_iv", 0)
        row = [col, info["type"], info.get("bin_mode",""), len(info["bins"]), iv, _iv_label(iv)]
        alt = ri % 2 == 0
        for ci, val in enumerate(row, 1):
            c = ws_sum.cell(row=ri, column=ci, value=val)
            sc(c, fill=_alt() if alt else _wht(),
               align="center" if ci > 1 else "left")
            if ci == 5: c.number_format = "0.0000"
            if ci == 6:
                c.fill = PatternFill("solid", fgColor=_iv_color(iv))
                c.font = Font(name="Arial", size=10, bold=True,
                              color="FFFFFFFF" if iv >= 0.1 else "FF000000")
        ws_sum.row_dimensions[ri].height = 16

    for ci, w in zip("ABCDEF", [24,13,13,8,12,14]):
        ws_sum.column_dimensions[ci].width = w
    ws_sum.freeze_panes = "A5"

    # ── One sheet per column ──────────────────────────────────────────
    for col, info in ranked:
        bins     = info["bins"]
        col_type = info["type"]
        is_num   = col_type == "numeric"
        total_iv = info.get("total_iv", 0)
        bin_mode = info.get("bin_mode", "")
        n_bins   = len(bins)
        sname    = col[:31]

        ws = wb.create_sheet(sname)
        ws.sheet_view.showGridLines = False

        # Title
        ws.merge_cells("A1:J1")
        c = ws["A1"]
        c.value     = col
        c.font      = Font(name="Arial", bold=True, size=13, color="FF1F3864")
        c.alignment = Alignment(horizontal="left", vertical="center")
        c.fill      = _wht()
        ws.row_dimensions[1].height = 24

        ws.merge_cells("A2:J2")
        c = ws["A2"]
        c.value = (f"Type: {col_type}   |   Mode: {bin_mode}   |   "
                   f"Total IV: {total_iv:.6f}   |   {_iv_label(total_iv)}")
        c.font      = Font(name="Arial", size=9, italic=True, color="FF666666")
        c.fill      = _wht()
        ws.row_dimensions[2].height = 14
        ws.row_dimensions[3].height = 6

        # Table headers row 4
        t_hdrs = ["#","Bin / Category","N","Events","Non-Events",
                  "Event Rate","WoE","IV","Cum. IV"]
        for ci, h in enumerate(t_hdrs, 1):
            c = ws.cell(row=4, column=ci, value=h)
            sc(c, bold=True, fill=_hdr(), fc="FFFFFFFF", align="center")
        ws.row_dimensions[4].height = 18

        # Data rows start at row 5
        DATA_START = 5
        cum_iv = 0.0
        for ri, b in enumerate(bins, DATA_START):
            cum_iv += b.get("iv", 0)
            lo, hi  = b.get("min",""), b.get("max","")
            label   = (f"[{lo:.4g}, {hi:.4g}]"
                       if is_num and isinstance(lo, float)
                       else b.get("label", str(lo)))
            row_data = [
                ri - DATA_START + 1, label,
                b.get("n",0), b.get("events",0), b.get("non_events",0),
                b.get("event_rate",0), b.get("woe",0), b.get("iv",0), cum_iv,
            ]
            alt = ri % 2 == 0
            for ci, val in enumerate(row_data, 1):
                c = ws.cell(row=ri, column=ci, value=val)
                sc(c, fill=_alt() if alt else _wht(),
                   align="right" if ci > 2 else ("center" if ci == 1 else "left"))
                if ci == 6: c.number_format = "0.00%"
                if ci in (7,8,9): c.number_format = "0.00000"
                if ci in (3,4,5): c.number_format = "#,##0"
            ws.row_dimensions[ri].height = 15

        last_data = DATA_START + n_bins - 1
        total_row = last_data + 1
        totals = {1:"Total", 3:f"=SUM(C{DATA_START}:C{last_data})",
                  4:f"=SUM(D{DATA_START}:D{last_data})",
                  5:f"=SUM(E{DATA_START}:E{last_data})",
                  8:f"=SUM(H{DATA_START}:H{last_data})"}
        for ci in range(1, 10):
            c = ws.cell(row=total_row, column=ci, value=totals.get(ci,""))
            sc(c, bold=True, fill=PatternFill("solid", fgColor="FFE2EFDA"),
               align="right")
            if ci == 3: c.number_format = "#,##0"
            if ci == 8: c.number_format = "0.00000"

        for ci, w in enumerate([4,26,10,10,12,12,10,10,10], 1):
            ws.column_dimensions[get_column_letter(ci)].width = w

        ws.freeze_panes = "A5"

        # Ensure sheet rels file exists (openpyxl omits it when no rels present)
        sheet_rels_path = f"xl/worksheets/_rels/sheet{len(chart_meta)+2}.xml.rels"
        # Will be written per-sheet after idx is known

        # Store chart metadata — chart goes in col K (11), row 4
        # count = col C (3), event_rate = col F (6)
        chart_meta[sname] = {
            "n_bins":        n_bins,
            "count_col":     3,   # C
            "er_col":        6,   # F
            "data_start_row": DATA_START,
            "anchor_col":    10,  # K (0-indexed)
            "anchor_row":    3,   # row 4 (0-indexed)
        }

    # ── Save workbook to bytes ────────────────────────────────────────
    buf = io.BytesIO()
    wb.save(buf)
    raw = buf.getvalue()

    # ── Inject combo chart XML into each column sheet ─────────────────
    def make_chart_xml(sheet_name, meta):
        n    = meta["n_bins"]
        ccol = get_column_letter(meta["count_col"])
        ecol = get_column_letter(meta["er_col"])
        r1   = meta["data_start_row"]
        r2   = r1 + n - 1
        sn   = sheet_name.replace("'", "\'")

        label_ref = f"\'{sn}\'!$B${r1}:$B${r2}"
        count_ref = f"\'{sn}\'!${ccol}${r1}:${ccol}${r2}"
        er_ref    = f"\'{sn}\'!${ecol}${r1}:${ecol}${r2}"

        # Unescaped for XML content
        lr = f"'{sn}'!$B${r1}:$B${r2}"
        cr = f"'{sn}'!${ccol}${r1}:${ccol}${r2}"
        er = f"'{sn}'!${ecol}${r1}:${ecol}${r2}"

        return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"
              xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
              xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <c:chart>
    <c:plotArea>
      <c:barChart>
        <c:barDir val="col"/>
        <c:grouping val="clustered"/>
        <c:ser>
          <c:idx val="0"/><c:order val="0"/>
          <c:tx><c:v>Count</c:v></c:tx>
          <c:spPr>
            <a:solidFill><a:srgbClr val="4472C4"/></a:solidFill>
            <a:ln><a:solidFill><a:srgbClr val="4472C4"/></a:solidFill></a:ln>
          </c:spPr>
          <c:cat><c:strRef><c:f>{lr}</c:f></c:strRef></c:cat>
          <c:val><c:numRef><c:f>{cr}</c:f></c:numRef></c:val>
        </c:ser>
        <c:gapWidth val="80"/>
        <c:axId val="1"/><c:axId val="2"/>
      </c:barChart>
      <c:lineChart>
        <c:grouping val="standard"/>
        <c:ser>
          <c:idx val="1"/><c:order val="1"/>
          <c:tx><c:v>Event Rate</c:v></c:tx>
          <c:spPr>
            <a:ln w="25400"><a:solidFill><a:srgbClr val="FF0000"/></a:solidFill></a:ln>
          </c:spPr>
          <c:marker>
            <c:symbol val="circle"/><c:size val="5"/>
            <c:spPr><a:solidFill><a:srgbClr val="FF0000"/></a:solidFill></c:spPr>
          </c:marker>
          <c:cat><c:strRef><c:f>{lr}</c:f></c:strRef></c:cat>
          <c:val><c:numRef><c:f>{er}</c:f></c:numRef></c:val>
          <c:smooth val="0"/>
        </c:ser>
        <c:axId val="1"/><c:axId val="3"/>
      </c:lineChart>
      <c:catAx>
        <c:axId val="1"/>
        <c:scaling><c:orientation val="minMax"/></c:scaling>
        <c:axPos val="b"/>
        <c:tickLblPos val="low"/>
        <c:crossAx val="2"/>
      </c:catAx>
      <c:valAx>
        <c:axId val="2"/>
        <c:scaling><c:orientation val="minMax"/></c:scaling>
        <c:axPos val="l"/>
        <c:title><c:tx><c:rich><a:bodyPr/><a:lstStyle/>
          <a:p><a:r><a:rPr lang="en-US"/><a:t>Count</a:t></a:r></a:p>
        </c:rich></c:tx><c:overlay val="0"/></c:title>
        <c:crossAx val="1"/>
      </c:valAx>
      <c:valAx>
        <c:axId val="3"/>
        <c:scaling><c:orientation val="minMax"/></c:scaling>
        <c:axPos val="r"/>
        <c:title><c:tx><c:rich><a:bodyPr/><a:lstStyle/>
          <a:p><a:r><a:rPr lang="en-US"/><a:t>Event Rate</a:t></a:r></a:p>
        </c:rich></c:tx><c:overlay val="0"/></c:title>
        <c:numFmt formatCode="0.0%" sourceLinked="0"/>
        <c:crossAx val="1"/>
        <c:crosses val="max"/>
      </c:valAx>
    </c:plotArea>
    <c:plotVisOnly val="1"/>
  </c:chart>
  <c:spPr>
    <a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill>
    <a:ln><a:solidFill><a:srgbClr val="D9D9D9"/></a:solidFill></a:ln>
  </c:spPr>
</c:chartSpace>""".encode("utf-8")

    def make_drawing_xml(anchor_col, anchor_row, chart_rel_id="rId1"):
        """Minimal drawing XML that places the chart at the given cell."""
        # Chart size: 18cm x 11cm in EMU (1cm = 914400/2.54 EMU)
        cx = int(18 * 914400 / 2.54)
        cy = int(11 * 914400 / 2.54)
        return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
          xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <xdr:oneCellAnchor>
    <xdr:from><xdr:col>{anchor_col}</xdr:col><xdr:colOff>0</xdr:colOff>
              <xdr:row>{anchor_row}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>
    <xdr:ext cx="{cx}" cy="{cy}"/>
    <xdr:graphicFrame macro="">
      <xdr:nvGraphicFramePr>
        <xdr:cNvPr id="2" name="Chart 1"/>
        <xdr:cNvGraphicFramePr/>
      </xdr:nvGraphicFramePr>
      <xdr:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></xdr:xfrm>
      <a:graphic>
        <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart">
          <c:chart xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"
                   r:id="{chart_rel_id}"/>
        </a:graphicData>
      </a:graphic>
    </xdr:graphicFrame>
    <xdr:clientData/>
  </xdr:oneCellAnchor>
</xdr:wsDr>""".encode("utf-8")

    def make_drawing_rels_xml(chart_path):
        return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart"
    Target="{chart_path}"/>
</Relationships>""".encode("utf-8")

    # Patch the zip
    in_zip  = zipfile.ZipFile(io.BytesIO(raw), "r")
    out_buf = io.BytesIO()
    out_zip = zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED)

    # Map sheet name → sheet index (1-based) from workbook.xml
    import re
    wb_xml = in_zip.read("xl/workbook.xml").decode()
    sheet_order = re.findall(r'name="([^"]+)"', wb_xml)
    sheet_idx   = {name: i+1 for i, name in enumerate(sheet_order)}

    # Files we will add (keyed by zip path)
    extra_files = {}
    # Track which [Content_Types].xml overrides to add
    ct_overrides = []

    chart_counter = 1
    for sname, meta in chart_meta.items():
        idx = sheet_idx.get(sname)
        if idx is None:
            continue

        chart_path    = f"xl/charts/chart{chart_counter}.xml"
        drawing_path  = f"xl/drawings/drawing{idx}.xml"
        draw_rel_path = f"xl/drawings/_rels/drawing{idx}.xml.rels"
        sheet_rel_path = f"xl/worksheets/_rels/sheet{idx}.xml.rels"

        # Chart XML
        extra_files[chart_path] = make_chart_xml(sname, meta)

        # Drawing XML
        extra_files[drawing_path] = make_drawing_xml(
            meta["anchor_col"], meta["anchor_row"])

        # Drawing rels (points drawing → chart)
        rel_target = f"../charts/chart{chart_counter}.xml"
        extra_files[draw_rel_path] = make_drawing_rels_xml(rel_target)

        # Sheet rels — always write fresh (openpyxl omits when sheet has no rels)
        draw_target = f"../drawings/drawing{idx}.xml"
        extra_files[sheet_rel_path] = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
            f'<Relationship Id="rId_draw{idx}" ' +
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" ' +
            f'Target="{draw_target}"/>' +
            '</Relationships>'
        ).encode("utf-8")

        ct_overrides.append(
            f'<Override PartName="/{chart_path}" ' +
            'ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"/>')
        ct_overrides.append(
            f'<Override PartName="/{drawing_path}" ' +
            'ContentType="application/vnd.openxmlformats-officedocument.drawing+xml"/>')

        chart_counter += 1

    # Now rewrite all files from original zip, patching sheet rels and [Content_Types]
    for item in in_zip.infolist():
        data = in_zip.read(item.filename)

        # Patch sheet{n}.xml to add drawing reference
        for sname, meta in chart_meta.items():
            idx = sheet_idx.get(sname)
            if not idx: continue
            if item.filename == f"xl/worksheets/sheet{idx}.xml":
                xml_str = data.decode("utf-8")
                draw_ref = f'<drawing r:id="rId_draw{idx}"/></worksheet>'
                if "<drawing" not in xml_str:
                    xml_str = xml_str.replace("</worksheet>", draw_ref)
                data = xml_str.encode("utf-8")

        # Patch [Content_Types].xml to register new parts
        if item.filename == "[Content_Types].xml":
            xml_str = data.decode("utf-8")
            for override in ct_overrides:
                if override not in xml_str:
                    xml_str = xml_str.replace("</Types>", override + "</Types>")
            data = xml_str.encode("utf-8")

        out_zip.writestr(item, data)

    # Write extra files
    for path, data in extra_files.items():
        out_zip.writestr(path, data)

    out_zip.close()
    return out_buf.getvalue()
