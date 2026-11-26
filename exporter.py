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
    OUTPUT_DIR, STATUS_LABELS,
    STATUS_MATCHED_LOWEST, STATUS_NEEDS_REVIEW, STATUS_ABOVE_LOWEST,
    STATUS_NO_HISTORY, STATUS_PARSE_ERROR,
)
import field_map

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
VIEW_HEADERS = ["序号", "供应商", "型号", "材质(展示)", "尺寸", "本单单价", "历史最低价",
                "状态", "依据", "复核原因", "来源文件", "行号"]
VIEW_WIDTHS = [6, 12, 22, 16, 16, 11, 12, 14, 46, 46, 22, 8]


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
        vals = [i, r.get("supplier"), r.get("model"), r.get("material"), r.get("size"),
                r.get("bill_price"), r.get("lowest_price"), STATUS_LABELS.get(r.get("status"), ""),
                r.get("evidence"), r.get("reason"), r.get("source_file"), r.get("source_row")]
        for j, v in enumerate(vals, 1):
            cell = ws.cell(row=row, column=j, value=v)
            cell.border = border
            cell.alignment = Alignment(vertical="center", wrap_text=(j in (9, 10)))
            if j in (6, 7) and isinstance(v, (int, float)):
                cell.number_format = "0.00"
            if color:
                cell.fill = PatternFill("solid", fgColor=color)
    ws.freeze_panes = "A3"
    if results:
        ws.auto_filter.ref = "A2:%s%d" % (get_column_letter(n), len(results) + 2)


def export_results(recon_no, results, batch):
    wb = Workbook()
    wb.properties.title = "账单核对结果_" + recon_no
    headers = _headers_of(batch, results)

    # 核对明细：严格按上传账单表头
    ws = wb.active
    ws.title = "核对明细"
    fill_header_output(ws, "核对明细 · 表头与上传账单完全一致（%d 列）" % len(headers),
                       headers, results)

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
        ("状态说明", "已匹配最低价=本单单价等于历史最低；需人工复核=同型号多档价/低于历史最低/无历史/解析失败；高于历史最低价=存在更低历史价"),
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
