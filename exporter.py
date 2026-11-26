# -*- coding: utf-8 -*-
"""结果导出：核对明细严格按用户上传账单表头输出（名称/顺序/数量一致），
另附状态视图与说明。核对产生的信息（状态/依据/原因等）保留在独立工作表，
不新增到按表头输出的明细表中。
"""
import os
import json
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from config import (
    OUTPUT_DIR, STATUS_LABELS, DIFF_LABELS,
    STATUS_MATCHED_LOWEST, STATUS_NEEDS_REVIEW, STATUS_ABOVE_LOWEST,
    STATUS_NO_HISTORY, STATUS_PARSE_ERROR,
)
import field_map
import recon_stats

STATUS_COLOR = {
    STATUS_MATCHED_LOWEST: "E2EFDA",
    STATUS_NEEDS_REVIEW: "FCE4D6",
    STATUS_ABOVE_LOWEST: "FFF2CC",
    STATUS_NO_HISTORY: "FCE4D6",
    STATUS_PARSE_ERROR: "FCE4E4",
}
C_HEAD = "FF4472C4"
C_HEAD_FG = "FFFFFFFF"
C_TITLE = "FF2F5597"
C_ERR = "FFFCE4E4"
C_BORDER = "FFBFBFBF"
thin = Side(style="thin", color=C_BORDER)
border = Border(left=thin, right=thin, top=thin, bottom=thin)

# 核对视角工作表（不属于按表头输出的明细）
# 智能对账：在原始 12 列基础上补「单价差异 / 差异率 / 数量 / 金额差异 / 基准期间 / 月份差 / 差异类型」
VIEW_HEADERS = ["序号", "供应商", "型号", "材质(展示)", "尺寸", "本单单价", "历史最低价",
                "单价差异", "差异率%", "数量", "金额差异", "基准期间", "月份差", "差异类型",
                "状态", "依据", "复核原因", "来源文件", "行号"]
VIEW_WIDTHS = [6, 12, 22, 16, 16, 11, 12, 11, 10, 9, 12, 10, 8, 20, 14, 52, 52, 22, 8]
# 金额类列（序号按 VIEW_HEADERS 从 1 开始）：单价/最低价/单价差异/差异率/数量/金额差异
MONEY_COLS = (6, 7, 8, 11)


def _diff_labels(raw):
    return "、".join(DIFF_LABELS.get(c, c) for c in str(raw or "").split(",") if c)


def _headers_of(batch, results):
    """取上传表头：优先批次记录，兼容旧数据从结果推断。"""
    hj = batch.get("headers_json")
    if hj:
        try:
            hs = json.loads(hj)
            if hs:
                return hs
        except Exception:
            pass
    # 旧批次无表头记录：用标准模板表头兜底
    return list(field_map.SYSTEM_TO_HEADER.values())


def fill_header_output(ws, title, headers, results):
    """按上传表头原样输出：字段名称、列顺序、列数量完全一致。"""
    n = len(headers)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=n)
    c = ws.cell(row=1, column=1, value=title)
    c.font = Font(bold=True, size=13, color=C_HEAD_FG)
    c.fill = PatternFill("solid", fgColor=C_TITLE)
    c.alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 24

    for j, h in enumerate(headers, 1):
        cell = ws.cell(row=2, column=j, value=h)   # 原样表头，不重命名
        cell.font = Font(bold=True, color=C_HEAD_FG)
        cell.fill = PatternFill("solid", fgColor=C_HEAD)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border
        ws.column_dimensions[get_column_letter(j)].width = max(12, min(24, len(str(h)) * 2 + 6))
    ws.row_dimensions[2].height = 26

    for i, r in enumerate(results, 1):
        row = i + 2
        raw = r.get("raw_values_json")
        try:
            vals = json.loads(raw) if raw else []
        except Exception:
            vals = []
        vals = field_map.row_by_header(vals, headers, None)
        status = r.get("status")
        bg = STATUS_COLOR.get(status)
        for j, v in enumerate(vals, 1):
            cell = ws.cell(row=row, column=j, value=v)
            cell.border = border
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            if bg:
                cell.fill = PatternFill("solid", fgColor="FF" + bg)
    ws.freeze_panes = "A3"
    if results:
        ws.auto_filter.ref = "A2:%s%d" % (get_column_letter(n), len(results) + 2)


def fill_view(ws, title, results, color=None):
    """核对视角工作表：展示状态、依据、复核原因等派生信息。"""
    n = len(VIEW_HEADERS)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=n)
    c = ws.cell(row=1, column=1, value=title)
    c.font = Font(bold=True, size=13, color=C_HEAD_FG)
    c.fill = PatternFill("solid", fgColor=C_TITLE)
    c.alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 24
    for j, h in enumerate(VIEW_HEADERS, 1):
        cell = ws.cell(row=2, column=j, value=h)
        cell.font = Font(bold=True, color=C_HEAD_FG)
        cell.fill = PatternFill("solid", fgColor=C_HEAD)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border
    for j, w in enumerate(VIEW_WIDTHS, 1):
        ws.column_dimensions[get_column_letter(j)].width = w
    for i, r in enumerate(results, 1):
        row = i + 2
        rate = r.get("gap_rate")
        vals = [i, r.get("supplier"), r.get("model"), r.get("material"), r.get("size"),
                r.get("bill_price"), r.get("lowest_price"), r.get("price_gap"),
                round(rate * 100, 2) if isinstance(rate, (int, float)) else None,
                r.get("quantity"), r.get("amount_gap"), r.get("hist_period"),
                r.get("date_gap"), _diff_labels(r.get("diff_types")),
                STATUS_LABELS.get(r.get("status"), ""),
                r.get("evidence"), r.get("reason"), r.get("source_file"), r.get("source_row")]
        for j, v in enumerate(vals, 1):
            cell = ws.cell(row=row, column=j, value=v)
            cell.border = border
            cell.alignment = Alignment(vertical="center", wrap_text=(j in (14, 16, 17)))
            if j in MONEY_COLS and isinstance(v, (int, float)):
                cell.number_format = "0.00"
            if j == 9 and isinstance(v, (int, float)):
                cell.number_format = "0.00"
            if j == 10 and isinstance(v, (int, float)):
                cell.number_format = "0.##"
            # 差异金额着色：超付红、少付绿（符合国内财务阅读习惯）
            if j in (8, 11) and isinstance(v, (int, float)) and abs(v) > 0.0001:
                cell.font = Font(color="FFC00000" if v > 0 else "FF2E7D32")
            if color:
                cell.fill = PatternFill("solid", fgColor=color)
    ws.freeze_panes = "A3"
    if results:
        ws.auto_filter.ref = "A2:%s%d" % (get_column_letter(n), len(results) + 2)


def _block_title(ws, row, text, ncols):
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=ncols)
    c = ws.cell(row=row, column=1, value=text)
    c.font = Font(bold=True, size=12, color=C_HEAD_FG)
    c.fill = PatternFill("solid", fgColor=C_TITLE)
    c.alignment = Alignment(vertical="center")
    ws.row_dimensions[row].height = 22


def _table_head(ws, row, headers):
    for j, h in enumerate(headers, 1):
        c = ws.cell(row=row, column=j, value=h)
        c.font = Font(bold=True, color=C_HEAD_FG)
        c.fill = PatternFill("solid", fgColor=C_HEAD)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = border
    ws.row_dimensions[row].height = 24


def fill_diff_summary(ws, summary):
    """差异汇总工作表：总体金额差异 + 按供应商 + 按差异类型（异常项汇总）。"""
    ncol = 11
    ws.column_dimensions["A"].width = 20
    for j in range(2, ncol + 1):
        ws.column_dimensions[get_column_letter(j)].width = 14

    row = 1
    _block_title(ws, row, "一、总体金额差异（金额单位：元）", ncol)
    row += 1
    amt = summary.get("amount", {})
    overall = [
        ("记录总数", summary.get("total", 0), "本次核对账单的行数"),
        ("本单金额合计", amt.get("bill_total", 0), "取自上传账单「合计金额」列（缺失时用 单价×数量 估算）"),
        ("应有金额合计", amt.get("should_total", 0), "历史最低价 × 数量，仅统计有数量的行"),
        ("超付合计", amt.get("overpay", 0), "本单高于历史最低价的金额之和（红色为多付）"),
        ("少付合计", amt.get("underpay", 0), "本单低于历史最低价的金额之和（负数）"),
        ("净差异", amt.get("net_gap", 0), "超付 + 少付"),
        ("有金额差异的笔数", amt.get("diff_rows", 0), "金额差异不为 0 的行数"),
        ("可换算金额的行数", amt.get("rows_with_quantity", 0), "同时具备数量与历史最低价的行数"),
    ]
    for k, v, note in overall:
        a = ws.cell(row=row, column=1, value=k); a.font = Font(bold=True); a.border = border
        b = ws.cell(row=row, column=2, value=v); b.border = border
        if isinstance(v, (int, float)) and not isinstance(v, bool) and k != "记录总数" and "笔数" not in k and "行数" not in k:
            b.number_format = "0.00"
            if k in ("超付合计",) and v:
                b.font = Font(color="FFC00000", bold=True)
        c = ws.cell(row=row, column=3, value=note); c.border = border
        c.alignment = Alignment(wrap_text=True, vertical="center")
        row += 1

    row += 1
    _block_title(ws, row, "二、按供应商汇总（按超付金额降序）", ncol)
    row += 1
    _table_head(ws, row, ["供应商", "记录数", "已匹配最低", "高于最低", "需复核",
                          "无历史", "解析失败", "有差异笔数", "超付合计", "少付合计", "最大差异率%"])
    row += 1
    for s in summary.get("by_supplier", []):
        vals = [s.get("supplier"), s.get("total"), s.get("matched"), s.get("above"),
                s.get("review"), s.get("nohistory"), s.get("error"), s.get("diff_rows"),
                s.get("overpay"), s.get("underpay"),
                round((s.get("max_gap_rate") or 0) * 100, 2)]
        for j, v in enumerate(vals, 1):
            c = ws.cell(row=row, column=j, value=v)
            c.border = border
            if j in (9, 10) and isinstance(v, (int, float)):
                c.number_format = "0.00"
                if j == 9 and v:
                    c.font = Font(color="FFC00000")
            if j == 11 and isinstance(v, (int, float)):
                c.number_format = "0.00"
        row += 1

    row += 1
    _block_title(ws, row, "三、按差异类型汇总（异常项分布）", ncol)
    row += 1
    _table_head(ws, row, ["差异类型", "涉及笔数", "超付合计", "少付合计", "说明"])
    ws.merge_cells(start_row=row, start_column=5, end_row=row, end_column=ncol)
    row += 1
    for d in summary.get("by_diff", []):
        note = {
            "amt_high": "本单单价高于历史最低价，存在可节省空间",
            "amt_low": "本单低于历史最低价，多为新高价或录入异常",
            "no_hist": "价格库无该型号，需补录历史价或单独议价",
            "parse_err": "单价为空/待核价等，无法参与比价",
            "multi_level": "同型号多档历史价，最低价是否适用需确认",
            "date_before": "账单月份早于最低价出现月份，属倒推比价",
            "base_stale": "最低价距今较久，价格可能已失效",
            "date_missing": "账单月份缺失，无法判断时效",
            "near_model": "型号近似命中（忽略符号差异），需确认是否同一物料",
        }.get(d.get("code"), "")
        vals = [d.get("label"), d.get("rows"), d.get("overpay"), d.get("underpay"), note]
        for j, v in enumerate(vals, 1):
            c = ws.cell(row=row, column=j, value=v)
            c.border = border
            if j in (3, 4) and isinstance(v, (int, float)):
                c.number_format = "0.00"
                if j == 3 and v:
                    c.font = Font(color="FFC00000")
            if j == 5:
                c.alignment = Alignment(wrap_text=True, vertical="center")
        ws.merge_cells(start_row=row, start_column=5, end_row=row, end_column=ncol)
        row += 1


def export_results(recon_no, results, batch):
    wb = Workbook()
    wb.properties.title = "账单核对结果_" + recon_no
    headers = _headers_of(batch, results)

    # 核对明细：严格按上传账单表头
    ws = wb.active
    ws.title = "核对明细"
    fill_header_output(ws, "核对明细 · 表头与上传账单完全一致（%d 列）" % len(headers),
                       headers, results)

    # 差异汇总（智能对账）：总体金额差异 / 按供应商 / 按差异类型
    summary = recon_stats.summarize(results)
    fill_diff_summary(wb.create_sheet("差异汇总"), summary)

    # 核对视角工作表（派生字段，不并入明细）
    rv = [r for r in results if r["status"] in (STATUS_NEEDS_REVIEW, STATUS_NO_HISTORY, STATUS_PARSE_ERROR)]
    fill_view(wb.create_sheet("需人工复核"), "需人工复核 · %d 条（橙色）" % len(rv), rv, "FFFCE4D6")

    mt = [r for r in results if r["status"] == STATUS_MATCHED_LOWEST]
    fill_view(wb.create_sheet("已匹配最低价"), "已匹配最低价 · %d 条（绿色）" % len(mt), mt, "FFE2EFDA")

    ab = [r for r in results if r["status"] == STATUS_ABOVE_LOWEST]
    fill_view(wb.create_sheet("高于历史最低价"), "高于历史最低价 · %d 条（黄色）" % len(ab), ab, "FFFFF2CC")

    # 说明
    diff = batch.get("mapping_diff_json")
    try:
        diff = json.loads(diff) if diff else []
    except Exception:
        diff = []
    lines = [
        ("核对批次", recon_no), ("来源文件", batch.get("file_name", "")),
        ("核对时间", batch.get("created_at", "")), ("总记录数", batch.get("total_rows", 0)),
        ("已匹配最低价", batch.get("n_matched", 0)),
        ("需人工复核", batch.get("n_review", 0) + batch.get("n_nohistory", 0) + batch.get("n_error", 0)),
        ("高于历史最低价", batch.get("n_above", 0)),
        ("核对明细表头", "严格取自上传账单原表头：%s（共 %d 列，未增删改名）"
         % (" / ".join(headers), len(headers))),
        ("字段映射差异", "；".join(diff) if diff else "无差异：上传表头全部字段均成功映射"),
        ("匹配规则", "按供应商+型号匹配；材质仅展示不参与；最低价为历史有效单价（排除空价/待核价/0价）的最小值"),
        ("差异口径", "单价差异 = 本单单价 − 历史最低价；金额差异 = 单价差异 × 数量；差异率 = 单价差异 / 历史最低价"),
        ("金额汇总", "本单金额合计 %.2f 元；应有金额合计（最低价×数量）%.2f 元；超付 %.2f 元、少付 %.2f 元"
         % (summary["amount"]["bill_total"], summary["amount"]["should_total"],
            summary["amount"]["overpay"], summary["amount"]["underpay"])),
        ("差异类型", "金额偏高 / 金额偏低 / 无历史基准 / 单价无法解析 / 多档价待定 / 日期早于基准 / 基准过期 / 月份缺失 / 型号近似匹配；"
                     "其中「型号近似匹配」是忽略符号差异后的命中，仅作参考、必须人工确认"),
        ("工作表说明", "「核对明细」表头严格等于上传账单；「差异汇总」给出总体金额差异、按供应商、按差异类型三块统计"),
        ("状态说明", "已匹配最低价=本单单价等于历史最低；需人工复核=同型号多档价/低于历史最低/近似型号/无历史/解析失败；高于历史最低价=存在更低历史价"),
        ("备注", "核对产生的状态/依据/复核原因等派生字段放在独立工作表，未加入「核对明细」表头，以保证明细与上传账单列完全一致"),
    ]
    ws5 = wb.create_sheet("说明")
    ws5.column_dimensions["A"].width = 18
    ws5.column_dimensions["B"].width = 96
    for i, (k, v) in enumerate(lines, 1):
        a = ws5.cell(row=i, column=1, value=k); a.font = Font(bold=True); a.border = border
        b = ws5.cell(row=i, column=2, value=v); b.alignment = Alignment(wrap_text=True, vertical="center"); b.border = border

    path = os.path.join(OUTPUT_DIR, "核对结果_%s.xlsx" % recon_no)
    wb.save(path)
    return path
