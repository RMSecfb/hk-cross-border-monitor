"""監控報告匯出：Excel 與 PDF（盤中、盤後共用）

版面一律是「摘要文字 → 統計 → 規則分布 → 明細表」，摘要放在所有表格之前。
由 streamlit_app.py 準備好 meta 後呼叫 build_excel / build_pdf。

meta 欄位：
  title        報告標題
  day_label    報告日期或時間標示
  summary_text 摘要段落（純文字，段落間以空行分隔）
  stats        [(項目, 數值), ...]
  rule_pivot   DataFrame，index 為監控類型，欄為 紅/橙/黃/合計
  detail       DataFrame，明細表
  detail_note  明細表上方的說明文字
  pdf_cols     PDF 明細要顯示的欄位（省略則用全部；欄位太多會擠成多行）
  pdf_levels   PDF 明細只收這些等級（省略則全收）
  pdf_rows     PDF 明細最多列數
  stats_first  True 時統計表排在摘要之前（今日高風險排行用）
  titles       覆寫區塊標題，如 {"detail": "今日高風險排行"}
"""
import io

LEVELS = ["紅", "橙", "黃"]
SEC = {"stats": "監控概況", "summary": "監控摘要", "rule": "規則分布", "detail": "預警列表"}


def _sec(meta, key):
    return (meta.get("titles") or {}).get(key, SEC[key])
HDR_BLUE = "1F4E79"
LV_FILL = {"紅": "FFCCCC", "橙": "FFE0B2", "黃": "FFF9C4"}
# PDF 明細欄寬偏好（mm），未列出的用預設值
COL_MM = {"股票代碼": 14, "股票名稱": 26, "等級": 10, "監控類型": 22, "指標值": 32,
          "閾值": 18, "風險分數": 12, "連續上榜(日)": 14, "近5日上榜(日)": 14,
          "時間": 16, "說明": 28, "當日觸發次數": 12, "備註": 24,
          "代碼": 14, "名稱": 26, "紅燈數": 10, "橙燈數": 10, "黃燈數": 10,
          "現價": 14, "漲跌幅%": 14, "成交額(萬)": 18, "觸發規則": 46}


# ---------------- Excel ----------------
def build_excel(meta):
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    thin = Border(*[Side(style="thin")] * 4)
    hdr_fill = PatternFill("solid", fgColor=HDR_BLUE)
    hdr_font = Font(bold=True, color="FFFFFF", size=11)

    # ---- Sheet1：摘要（文字在最上方，其後才是統計表）----
    ws = wb.active
    ws.title = "監控摘要"
    ws.merge_cells("A1:E1")
    ws["A1"] = meta["title"]
    ws["A1"].font = Font(bold=True, size=14)
    ws["A1"].alignment = Alignment(horizontal="center")
    ws.merge_cells("A2:E2")
    ws["A2"] = meta["day_label"]
    ws["A2"].font = Font(size=10, color="666666")
    ws["A2"].alignment = Alignment(horizontal="center")

    def write_summary(r):
        for para in [p for p in meta["summary_text"].split("\n") if p.strip()]:
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=5)
            c = ws.cell(r, 1, para.strip())
            c.alignment = Alignment(wrap_text=True, vertical="top")
            ws.row_dimensions[r].height = 32
            r += 1
        return r + 1

    def write_stats(r):
        ws.cell(r, 1, "統計項目").fill = hdr_fill
        ws.cell(r, 1).font = hdr_font
        ws.cell(r, 2, "數值").fill = hdr_fill
        ws.cell(r, 2).font = hdr_font
        r += 1
        for k, v in meta["stats"]:
            ws.cell(r, 1, k).border = thin
            ws.cell(r, 2, v).border = thin
            r += 1
        return r

    r = 4
    r = write_stats(r) + 1 if meta.get("stats_first") else write_summary(r)
    r = write_summary(r) if meta.get("stats_first") else write_stats(r)

    rp = meta["rule_pivot"]
    if rp is not None and not rp.empty:
        r += 1
        for i, hname in enumerate(["規則", *LEVELS, "合計"], 1):
            c = ws.cell(r, i, hname)
            c.fill = hdr_fill
            c.font = hdr_font
        r += 1
        for name, row in rp.iterrows():
            ws.cell(r, 1, name).border = thin
            for i, lv in enumerate(LEVELS, 2):
                ws.cell(r, i, int(row.get(lv, 0))).border = thin
            ws.cell(r, 5, int(row.get("合計", 0))).border = thin
            r += 1

    ws.column_dimensions["A"].width = 34
    for col in "BCDE":
        ws.column_dimensions[col].width = 14

    # ---- Sheet2：明細 ----
    det = meta["detail"]
    ws2 = wb.create_sheet("預警列表明細")
    headers = list(det.columns)
    ws2.append(headers)
    for i in range(1, len(headers) + 1):
        c = ws2.cell(1, i)
        c.fill, c.font, c.border = hdr_fill, hdr_font, thin
        c.alignment = Alignment(horizontal="center")
    # 明細可能上萬列：逐格套框線會非常慢，只替「等級」欄上底色
    fills = {k: PatternFill("solid", fgColor=v) for k, v in LV_FILL.items()}
    lv_i = headers.index("等級") + 1 if "等級" in headers else None
    for row in det.itertuples(index=False, name=None):
        ws2.append(row)
        if lv_i:
            f = fills.get(row[lv_i - 1])
            if f:
                ws2.cell(ws2.max_row, lv_i).fill = f
    widths = {"預警ID": 18, "時間": 10, "等級": 8, "股票代碼": 9, "股票名稱": 18,
              "監控類型": 14, "指標值": 20, "閾值": 14, "當日觸發次數": 10, "說明": 20,
              "風險分數": 9, "連續上榜(日)": 11, "近5日上榜(日)": 11, "操作建議": 40, "發現批次": 18}
    for i, name in enumerate(headers, 1):
        ws2.column_dimensions[get_column_letter(i)].width = widths.get(name, 14)
    ws2.freeze_panes = "A2"
    ws2.auto_filter.ref = ws2.dimensions

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------- PDF ----------------
def build_pdf(meta):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        fn = "STSong-Light"
    except Exception:
        fn = "Helvetica"

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm)
    S = lambda n, **kw: ParagraphStyle(n, fontName=fn, **kw)
    st_title = S("t", fontSize=16, leading=20, spaceAfter=4, alignment=1)
    st_sub = S("s", fontSize=10, leading=14, spaceAfter=10, alignment=1, textColor=colors.grey)
    st_body = S("b", fontSize=9.5, leading=15, spaceAfter=6)
    st_note = S("n", fontSize=8.5, leading=12, spaceAfter=4, textColor=colors.HexColor("#555555"))
    st_h = S("h", fontSize=11.5, leading=15, spaceBefore=12, spaceAfter=5,
             textColor=colors.HexColor("#1F4E79"))
    st_cell = S("c", fontSize=7, leading=9, alignment=1)
    st_cell_h = S("ch", fontSize=7.5, leading=10, alignment=1, textColor=colors.white)

    def grid(data, widths, header=True):
        t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
        sty = [("FONTNAME", (0, 0), (-1, -1), fn), ("FONTSIZE", (0, 0), (-1, -1), 9),
               ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
               ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
               ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F5F5")])]
        if header:
            sty += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E79")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white)]
        t.setStyle(TableStyle(sty))
        return t

    story = [Paragraph(meta["title"], st_title), Paragraph(meta["day_label"], st_sub)]
    num = iter(["一", "二", "三", "四"])

    def stats_block():
        sd = [["統計項目", "數值"]] + [[k, str(v)] for k, v in meta["stats"]]
        t = grid(sd, [80 * mm, 40 * mm])
        t.setStyle(TableStyle([("ALIGN", (1, 0), (1, -1), "CENTER")]))
        return [Spacer(1, 3 * mm), t]

    def summary_block():
        out = [Paragraph(f"{next(num)}、{_sec(meta, 'summary')}", st_h)]
        out += [Paragraph(p.strip(), st_body)
                for p in meta["summary_text"].split("\n") if p.strip()]
        return out

    if meta.get("stats_first"):          # 統計在前：畫面上方那一排指標先呈現
        story += [Paragraph(f"{next(num)}、{_sec(meta, 'stats')}", st_h)] + stats_block()
        story += summary_block()
    else:
        story += summary_block() + stats_block()

    rp = meta["rule_pivot"]
    if rp is not None and not rp.empty:
        story.append(Paragraph(f"{next(num)}、{_sec(meta, 'rule')}", st_h))
        rd = [["規則", *LEVELS, "合計"]]
        for name, row in rp.iterrows():
            rd.append([name, *[str(int(row.get(lv, 0))) for lv in LEVELS], str(int(row.get("合計", 0)))])
        t = grid(rd, [60 * mm, 20 * mm, 20 * mm, 20 * mm, 20 * mm])
        t.setStyle(TableStyle([("ALIGN", (1, 0), (-1, -1), "CENTER")]))
        story.append(t)

    # 三、明細
    det, nmax = meta["detail"], meta.get("pdf_rows", 200)
    story.append(Paragraph(f"{next(num)}、{_sec(meta, 'detail')}", st_h))
    if meta.get("detail_note"):
        story.append(Paragraph(meta["detail_note"], st_note))
    # PDF 寬度有限，欄位太多會把每列擠成好幾行；只放重點欄位，完整欄位在 Excel
    cols = [c for c in meta.get("pdf_cols") or det.columns if c in det.columns]
    det = det[cols]
    lv_only = meta.get("pdf_levels")          # 例如盤中只收紅、橙
    if lv_only and "等級" in det.columns:
        det = det[det["等級"].isin(lv_only)]
    sub = det.head(nmax)
    raw = [[str(v) for v in row] for _, row in sub.iterrows()]
    data = [[Paragraph(c, st_cell_h) for c in cols]] + \
           [[Paragraph(v, st_cell) for v in row] for row in raw]
    pref = [COL_MM.get(c, 18) for c in cols]
    scale = 180.0 / sum(pref)
    widths = [w * scale * mm for w in pref]
    t = grid(data, widths)
    if "等級" in cols:
        li = cols.index("等級")
        extra = []
        for ri, row in enumerate(raw, 1):
            hexc = LV_FILL.get(row[li])
            if hexc:
                extra.append(("BACKGROUND", (li, ri), (li, ri), colors.HexColor("#" + hexc)))
        t.setStyle(TableStyle(extra))
    story.append(t)

    footer = f"{meta['title']}　{meta['day_label']}"

    def page_num(canvas, d):
        canvas.saveState()
        canvas.setFont(fn, 8)
        canvas.setFillColor(colors.grey)
        canvas.drawRightString(d.pagesize[0] - 15 * mm, 10 * mm, f"第 {d.page} 頁　{footer}")
        canvas.restoreState()

    doc.build(story, onFirstPage=page_num, onLaterPages=page_num)
    return buf.getvalue()
