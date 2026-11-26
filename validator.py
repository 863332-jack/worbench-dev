# -*- coding: utf-8 -*-
"""严格数据审查：上传前按标准录入模板逐项校验，任何错误即整批拒绝。

标准模板列序（历史账单录入模板.xlsx）：
供应商 / 日期 / 型号 / 尺寸 / 材质 / 数量 / 单价 / 合计金额 / 备注
带 * 为必填：供应商、日期、型号、单价。

错误对象统一字段：file, row, field, value, reason, cause
  reason = 具体问题；cause = 可能原因与修正建议
"""
import re
from datetime import date, datetime

import parsers
import field_map


def cell_json_value(v):
    """把单元格值转为 JSON 可序列化形式：日期转 YYYY-MM-DD，其它保持。"""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S") if (v.hour or v.minute or v.second) else v.strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    return v

TEMPLATE_HEADERS = ["供应商", "日期", "型号", "尺寸", "材质", "数量", "单价", "合计金额", "备注"]
REQUIRED = ("供应商", "日期", "型号", "单价")
SUPPLIERS = ("杰盛", "新新精密", "云锦")
EXCLUDE_WORDS = ("待核", "待确认", "待定", "报价", "不详", "N/A", "NA")

NUM_RE = re.compile(r"^\d+(?:\.\d+)?$")
DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日", "%Y年%m月", "%Y-%m")


def norm_header(s):
    return re.sub(r"[\s*＊]", "", parsers.cell_str(s))


def _parse_date(v):
    if isinstance(v, datetime):
        return (v.year, v.month, v.day)
    if isinstance(v, date):
        return (v.year, v.month, v.day)   # 修复 P3-16：纯 date 对象不再丢失日
    if isinstance(v, (int, float)) and 30000 < float(v) < 60000:
        try:
            import openpyxl
            d = openpyxl.utils.datetime.from_excel(float(v))
            return (d.year, d.month, d.day)
        except Exception:
            return None
    s = parsers.cell_str(v)
    if not s:
        return None
    for fmt in DATE_FORMATS:
        try:
            d = datetime.strptime(s, fmt)
            return (d.year, d.month, d.day)
        except ValueError:
            continue
    m = re.match(r"^(20\d{2})[-/.年](\d{1,2})(?:[-/.月](\d{1,2}))?", s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        dd = int(m.group(3)) if m.group(3) else 1
        if 1 <= mo <= 12 and 1 <= dd <= 31:
            return (y, mo, dd)
    return None


def _header_diffs(rows):
    """在前 20 行找最接近模板的表头，返回 (行号, 差异列表)。
    差异项: (列序号1based, 期望列名, 实际列名)。完全匹配时差异列表为空。
    """
    best = None
    for i, row in enumerate(rows[:20]):
        cells = [norm_header(c) for c in row]
        while cells and cells[-1] == "":
            cells.pop()
        diffs = []
        for j, expected in enumerate(TEMPLATE_HEADERS):
            actual = cells[j] if j < len(cells) else "(缺失)"
            if actual != expected:
                diffs.append((j + 1, expected, actual))
        if len(cells) > len(TEMPLATE_HEADERS):
            for j in range(len(TEMPLATE_HEADERS), len(cells)):
                diffs.append((j + 1, "(无此列)", cells[j]))
        if best is None or len(diffs) < len(best[1]):
            best = (i, diffs)
        if not diffs:
            return i, []
    return best if best else (None, [])


def validate_file(path, supplier_override="", kind="history"):
    fname = path.split("/")[-1]
    errors, warnings = [], []

    def err(row, field, value, reason, cause=""):
        errors.append({"file": fname, "row": row, "field": field,
                       "value": str(value) if value is not None else "",
                       "reason": reason, "cause": cause})

    def warn(row, field, value, reason, cause=""):
        warnings.append({"file": fname, "row": row, "field": field,
                         "value": str(value) if value is not None else "",
                         "reason": reason, "cause": cause})

    try:
        _wb, sheet, rows = parsers.open_sheet(path)
    except Exception as e:
        err(0, "文件", "", "无法读取文件：%s" % e,
            "请确认文件未损坏、未被加密，且为 .xlsx/.xls 格式；可另存为新文件后重试。")
        return {"ok": False, "errors": errors, "warnings": warnings, "rows": [], "meta": {}}

    hi, hdiffs = _header_diffs(rows)
    if hi is None or hdiffs:
        if hdiffs:
            detail = "；".join("第%d列 应为「%s」实际「%s」" % d for d in hdiffs[:9])
            err(0, "表头", "", "表头不符合标准录入模板：" + detail,
                "请直接使用 deliverables/历史账单录入模板.xlsx 录入；不要增删列、改名或调整列顺序。")
        else:
            err(0, "表头", "", "未找到标准模板表头（前 20 行内）。",
                "请在「账单数据」工作表第 2 行按模板填写 9 列表头，再删除示例行录入数据。")
        return {"ok": False, "errors": errors, "warnings": warnings, "rows": [], "meta": {}}

    header_excel_row = hi + 1
    # 上传账单的真实表头（原样保留，含 * 与列顺序、列数量）
    raw_headers = [parsers.cell_str(c) for c in rows[hi]]
    while raw_headers and raw_headers[-1] == "":
        raw_headers.pop()
    # 建立字段映射与差异说明
    mapping = field_map.build_mapping(raw_headers)
    mapping_diff = field_map.describe_diff(mapping, raw_headers)

    clean = []
    seen_example = False

    for idx in range(hi + 1, len(rows)):
        row = rows[idx]
        excel_row = idx + 1
        if all(c is None or parsers.cell_str(c) == "" for c in row):
            continue
        # 原始行值（按真实表头列顺序，原样保留，用于核对明细按表头输出）
        raw_values = [cell_json_value(row[j]) if j < len(row) else None for j in range(len(raw_headers))]
        vals = [row[j] if j < len(row) else None for j in range(len(TEMPLATE_HEADERS))]
        rec = dict(zip(TEMPLATE_HEADERS, vals))

        remark = parsers.cell_str(rec["备注"])
        if "示例" in remark:
            seen_example = True
            continue

        row_ok = True

        # 供应商
        supplier = parsers.cell_str(rec["供应商"]).strip()
        if not supplier:
            err(excel_row, "供应商", "", "必填项「供应商」为空",
                "可能漏填；请下拉选择 杰盛 / 新新精密 / 云锦 之一。")
            row_ok = False
        elif supplier not in SUPPLIERS:
            err(excel_row, "供应商", supplier, "供应商必须是：%s 之一" % " / ".join(SUPPLIERS),
                "常见原因：使用简称/别名（如「云景」「新新」）、错别字或多余空格；请用下拉框重新选择。")
            row_ok = False
        elif supplier_override and supplier_override != supplier:
            err(excel_row, "供应商", supplier,
                "与上传时选择的供应商「%s」不一致" % supplier_override,
                "请把本行供应商改为与页面所选一致，或清空页面供应商选择后重新上传。")
            row_ok = False

        # 日期
        d = _parse_date(rec["日期"])
        date_raw = parsers.cell_str(rec["日期"])
        if not date_raw:
            err(excel_row, "日期", "", "必填项「日期」为空",
                "可能漏填或列错位；请填写账单日期，格式 YYYY-MM-DD（如 2026-07-03）。")
            row_ok = False
        elif d is None:
            err(excel_row, "日期", date_raw,
                "日期格式错误，应为 YYYY-MM-DD（如 2026-07-03）",
                "常见原因：只写了「7月1」缺年份、写成中文“七月”、或单元格为文本/含多余字符；请改为标准日期。")
            row_ok = False
        elif not (2000 <= d[0] <= 2100 and 1 <= d[1] <= 12 and 1 <= d[2] <= 31):
            err(excel_row, "日期", date_raw, "日期值超出合理范围（年份 2000-2100）",
                "可能年份或月份输入错误；请核对原始账单日期。")
            row_ok = False

        # 型号
        model = parsers.cell_str(rec["型号"]).strip()
        if not model:
            err(excel_row, "型号", "", "必填项「型号」为空",
                "可能漏填，或把型号误写到「尺寸/材质」列；型号是匹配主键，必须填写。")
            row_ok = False

        # 单价
        price_raw = parsers.cell_str(rec["单价"]).strip()
        price = None
        if not price_raw:
            err(excel_row, "单价", "", "必填项「单价」为空",
                "可能漏填，或从原账单直接复制了空价行；单价必须为正数数字。")
            row_ok = False
        elif any(w.lower() in price_raw.lower() for w in EXCLUDE_WORDS):
            err(excel_row, "单价", price_raw, "单价含「待核价/待定/报价」等排除标记，不允许录入",
                "该行在原始账单中尚未核价；请先确认实际单价后填写数字，或删除该行。")
            row_ok = False
        elif not NUM_RE.match(price_raw):
            err(excel_row, "单价", price_raw,
                "单价格式错误，必须是正数数字（如 4 或 3.5），不能含文字/货币符号",
                "常见原因：填了「4元」「￥4」「约4」等；只保留数字，不要单位符号。")
            row_ok = False
        else:
            price = float(price_raw)
            if price <= 0:
                err(excel_row, "单价", price_raw, "单价必须大于 0，当前为 0 或负数",
                    "可能用 0 占位或误输入负号；请填实际成交单价。")
                row_ok = False
            elif price > 100000:
                err(excel_row, "单价", price_raw, "单价 %s 异常（>100000）" % price_raw,
                    "常见原因：把「合计金额」误填到单价列；请核对原始账单。")
                row_ok = False

        # 数量
        qty_raw = parsers.cell_str(rec["数量"]).strip()
        qty = None
        if qty_raw:
            if not NUM_RE.match(qty_raw):
                err(excel_row, "数量", qty_raw, "数量格式错误，必须是数字（如 300）",
                    "常见原因：填了「三百」「300个」等；只保留数字。")
                row_ok = False
            else:
                qty = float(qty_raw)
                if qty < 0:
                    err(excel_row, "数量", qty_raw, "数量不能为负数",
                        "请核对原始账单数量，不应为负。")
                    row_ok = False

        # 合计金额
        total_raw = parsers.cell_str(rec["合计金额"]).strip()
        total = None
        if total_raw:
            if not NUM_RE.match(total_raw):
                err(excel_row, "合计金额", total_raw, "合计金额格式错误，必须是数字（如 1800）",
                    "常见原因：填了「1800元」「￥1800」等；只保留数字。")
                row_ok = False
            else:
                total = float(total_raw)
                if total < 0:
                    err(excel_row, "合计金额", total_raw, "合计金额不能为负数",
                        "请核对原始账单金额，不应为负。")
                    row_ok = False

        if row_ok and qty is not None and total is not None and price is not None:
            expect = round(qty * price, 2)
            if abs(total - expect) > max(1.0, expect * 0.01):
                warn(excel_row, "合计金额", total_raw,
                     "合计金额与「数量×单价 ≈ %s」不一致，请核对" % expect,
                     "可能存在含税/折扣/四舍五入；此项只提示不拦截。")

        if row_ok:
            clean.append({
                "supplier": supplier, "model": model,
                "model_norm": parsers.normalize_model(model),
                "material": parsers.cell_str(rec["材质"]).strip(),
                "size": parsers.cell_str(rec["尺寸"]).strip(),
                "quantity": qty, "total_amount": total, "unit_price": price,
                "period": "%04d-%02d" % (d[0], d[1]) if d else "",
                "date": "%04d-%02d-%02d" % d if d else "",
                "remark": remark, "source_file": fname, "source_row": excel_row,
                "raw_values": raw_values,
            })

    if seen_example:
        warn(0, "示例行", "", "检测到模板示例行（备注含「示例」）已自动跳过，未录入系统",
             "正式录入前请删除该行，避免误导入示例数据。")

    if not clean and not errors:
        err(0, "数据", "", "文件中没有可录入的有效数据行",
            "请从第 4 行开始逐行填写数据，并删除模板示例行。")

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "rows": clean,
        "meta": {"supplier": supplier_override or (clean[0]["supplier"] if clean else ""),
                 "header_row": header_excel_row, "sheet": sheet,
                 "headers": raw_headers,
                 "mapping_diff": mapping_diff,
                 "total": sum(1 for idx in range(hi + 1, len(rows))
                              if any(c is not None and parsers.cell_str(c) != "" for c in rows[idx])),
                 "valid": len(clean)},
    }
