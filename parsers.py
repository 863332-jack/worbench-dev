# -*- coding: utf-8 -*-
"""账单解析器：识别供应商、定位表头、提取字段、校验。
设计要点（来自真实账单实证）：
- 杰盛：表头第2行，型号/材质/序号(实为尺寸)/单价；8月列序与7/9月不同 → 按列名定位
- 新新精密：名称/规格列把型号+材质+尺寸混在一格 → 拆分；日期列为 Excel 序列号，标题含年份
- 云锦：产品名称=型号，规格/名称=尺寸，材质，单价；只读 Sheet1（「核对」表是辅助表，单价被改过）
- 标准化表：含列「型号/材质/尺寸/单价/月份」，作为新账单核对入口的通用格式
"""
import os
import re
from datetime import date, datetime

from config import SUPPLIER_KEYS, EXCLUDE_MARKERS


def xl_color_unused():
    pass


def _is_sheet1(name):
    return name.strip().lower().replace(" ", "") == "sheet1"


# 标准账单表头关键词：供应商/日期/型号/单价（必填四项）同时出现的行即为主数据表
_BILL_HEADER_KEYS = ("供应商", "日期", "型号", "单价")


def rows_have_bill_header(rows, top=20):
    """判定行集合前 top 行内是否存在标准账单表头行。

    修复缺陷 P1-1：模板含「账单数据」+「填写说明」两个工作表时，
    旧逻辑按"非空行数最多"选表，稀疏数据（录入行数 < 说明行数）会误选
    「填写说明」导致误拒。现优先按表头特征选表，表头感知优先于密度启发式。
    """
    for row in rows[:top]:
        cells = [norm_header(c) for c in row]
        hits = sum(1 for k in _BILL_HEADER_KEYS if any(k in c for c in cells))
        if hits >= len(_BILL_HEADER_KEYS):
            return True
    return False


def open_sheet(path):
    """返回 (workbook, sheet_name, rows_as_list)。兼容 xlsx/xls。

    选表优先级：
    1. 含标准账单表头（供应商/日期/型号/单价同行）的工作表 —— 表头感知；
    2. 名为 Sheet1 且含数据的工作表（避免误选「核对」等辅助表）；
    3. 前 30 行非空行数最多的工作表（兜底密度启发式）。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        # ① 表头感知：优先选择包含标准账单表头的工作表
        name = None
        for ws in wb.worksheets:
            try:
                head_rows = [list(r) for r in ws.iter_rows(min_row=1, max_row=25, values_only=True)]
            except Exception:
                continue
            if rows_have_bill_header(head_rows):
                name = ws.title
                break
        # ② Sheet1 优先（若含数据）
        if name is None:
            for nm in wb.sheetnames:
                if _is_sheet1(nm):
                    try:
                        ws0 = wb[nm]
                        if any(any(c not in (None, "") for c in row)
                               for row in ws0.iter_rows(min_row=1, max_row=min(ws0.max_row, 15), values_only=True)):
                            name = nm
                    except Exception:
                        pass
                    break
        # ③ 密度兜底
        if name is None:
            best = None
            for ws in wb.worksheets:
                try:
                    n = sum(1 for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 30), values_only=True)
                            if any(c not in (None, "") for c in row))
                    if best is None or n > best[1]:
                        best = (ws.title, n)
                except Exception:
                    continue
            name = best[0] if best else wb.sheetnames[0]
        ws = wb[name]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        return wb, name, rows
    elif ext == ".xls":
        import xlrd
        wb = xlrd.open_workbook(path)
        # ① 表头感知
        name = None
        for sh in wb.sheets():
            head_rows = [[sh.cell_value(r, c) for c in range(sh.ncols)] for r in range(min(sh.nrows, 25))]
            if rows_have_bill_header(head_rows):
                name = sh.name
                break
        # ② Sheet1 优先
        if name is None:
            for nm in wb.sheet_names():
                if _is_sheet1(nm):
                    sh0 = wb.sheet_by_name(nm)
                    if any(any(str(sh0.cell_value(r, c)).strip() for c in range(sh0.ncols))
                           for r in range(min(sh0.nrows, 15))):
                        name = nm
                    break
        # ③ 密度兜底
        if name is None:
            best = None
            for sh in wb.sheets():
                n = 0
                for r in range(min(sh.nrows, 30)):
                    if any(str(sh.cell_value(r, c)).strip() for c in range(sh.ncols)):
                        n += 1
                if best is None or n > best[1]:
                    best = (sh.name, n)
            name = best[0] if best else wb.sheet_names()[0]
        sh = wb.sheet_by_name(name)
        rows = [[sh.cell_value(r, c) for c in range(sh.ncols)] for r in range(sh.nrows)]
        return wb, name, rows
    else:
        raise ValueError("不支持的文件格式: " + ext + "（仅支持 .xlsx / .xlsm / .xls）")


def cell_str(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def norm_header(s):
    """表头归一化：去掉内部空白，如「日   期」→「日期」。仅用于表头匹配。"""
    return re.sub(r"\s+", "", cell_str(s))


def extract_title_year(rows):
    """从账单标题行（前10行）提取「年-月」，作为明细日期缺失/缺年份时的 fallback。"""
    for r in rows[:10]:
        for c in r:
            m = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月", cell_str(c))
            if m:
                return "%04d-%02d" % (int(m.group(1)), int(m.group(2)))
    return ""


def detect_supplier(path, rows):
    """按文件名优先、表头内容次之识别供应商。"""
    base = os.path.basename(path)
    for key, val in SUPPLIER_KEYS.items():
        if key in base:
            return val
    head = " ".join(cell_str(c) for r in rows[:6] for c in r)
    for key, val in SUPPLIER_KEYS.items():
        if key in head:
            return val
    return "未知"


def find_header(rows, must_have):
    """找到包含所有 must_have 关键词的表头行，返回 (行号, {列索引:列名})。
    匹配前对表头做空白归一化，兼容「日   期」「数 量」等写法。
    """
    for i, row in enumerate(rows[:20]):
        cells = [norm_header(c) for c in row]
        if all(any(k in c for c in cells) for k in must_have):
            mapping = {j: c for j, c in enumerate(cells) if c}
            return i, mapping
    return None, {}


def col_index(mapping, *keywords, exclude=()):
    for idx, name in mapping.items():
        if any(k in name for k in keywords) and not any(x in name for x in exclude):
            return idx
    return None


def parse_unit_price(v):
    """解析单价：返回 (值, 排除原因)。"""
    s = cell_str(v)
    if not s or s in ("-", "—", "/"):
        return None, "空价"
    if any(m in s for m in EXCLUDE_MARKERS):
        return None, "待核价"
    # 去掉货币符号、千分位
    s2 = s.replace(",", "").replace("￥", "").replace("¥", "").replace("元", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s2)
    if not m:
        return None, "单价格式异常"
    try:
        val = float(m.group())
    except ValueError:
        return None, "单价格式异常"
    if val <= 0:
        return None, "零价/负价"
    return round(val, 4), None


def parse_period(v, fallback=""):
    s = cell_str(v)
    if not s:
        return fallback
    # Excel 日期序列号
    if isinstance(v, (int, float)) and 30000 < float(v) < 60000:
        try:
            import openpyxl
            d = openpyxl.utils.datetime.from_excel(float(v))
            return "%04d-%02d" % (d.year, d.month)
        except Exception:
            pass
    m = re.search(r"(20\d{2})[.\-/年]\s*(\d{1,2})", s)
    if m:
        return "%04d-%02d" % (int(m.group(1)), int(m.group(2)))
    m = re.search(r"(\d{1,2})\s*月", s)
    if m:
        mm = int(m.group(1))
        if len(fallback) == 7 and fallback[4] == "-":  # fallback 已含年-月，如 2026-09
            return fallback
        return fallback + "-%02d" % mm if fallback else "%02d月" % mm
    # 单元格为空或仅含年份文字：回退到标题的「年-月」
    if fallback:
        return fallback
    return s[:16]


# ---------- 型号拆分（新新精密专用） ----------
DIM_RE = re.compile(r"^[DdHh∅Φφ]?\s*\d+(?:\.\d+)?\s*[*×xX]\s*\d+(?:\.\d+)?$")
MODEL_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*[-—]?[A-Za-z0-9]*")


def split_xinxi_name(text):
    """从「名称/规格」混排文本拆出 (型号, 材质, 尺寸)。"""
    s = cell_str(text)
    # 括号内视作材质
    mats = re.findall(r"[（(]([^）)]+)[）)]", s)
    material = mats[-1].strip() if mats else ""
    cleaned = re.sub(r"[（(][^）)]*[）)]", " ", s)
    tokens = [t for t in re.split(r"\s+", cleaned) if t]
    # 尺寸 token：形如 D34*8 / H18×55 / ∅29×156
    size = ""
    model = ""
    for t in tokens:
        if DIM_RE.match(t.replace(" ", "")):
            size = t
    # 型号 token：含字母、非纯尺寸
    candidates = []
    for t in tokens:
        if DIM_RE.match(t.replace(" ", "")):
            continue
        for m in MODEL_TOKEN_RE.finditer(t):
            cand = m.group()
            if len(cand) >= 2 and any(ch.isalpha() for ch in cand):
                candidates.append(cand)
    if candidates:
        model = candidates[-1]
    if not model:
        model = s  # 保留原文，标解析状态
    return model, material, size


# ---------- 通用解析 ----------
def parse_rows(path, supplier=None, kind="history"):
    """解析一个文件，返回 (rows, meta)。
    rows: list dict with keys supplier/model/model_norm/material/size/unit_price/period/
          source_row/exclude_reason
    meta: supplier, sheet, header_row, total, valid, excluded
    """
    wb, sheet, rows = open_sheet(path)
    supplier_override = supplier
    supplier = supplier or detect_supplier(path, rows)
    title_year = extract_title_year(rows)

    def build(model, material, size, price_v, period, r_idx, norm=None, row_supplier=None):
        val, reason = parse_unit_price(price_v)
        m = cell_str(model)
        return {
            "supplier": row_supplier or supplier,
            "model": m,
            "model_norm": norm or normalize_model(m),
            "material": cell_str(material),
            "size": cell_str(size),
            "unit_price": val,
            "period": parse_period(period, fallback=title_year),
            "source_file": os.path.basename(path),
            "source_row": r_idx + 1,
            "exclude_reason": reason,
        }

    # 标准表 / 已整理表：含「型号」和「单价」列
    hi, mp = find_header(rows, ["型号", "单价"])
    if hi is not None:
        c_model = col_index(mp, "型号")
        c_price = col_index(mp, "单价")
        c_mat = col_index(mp, "材质")
        c_size = col_index(mp, "尺寸", "规格", "序号", exclude=("名称",))
        c_period = col_index(mp, "月份", "日期", "时间")
        c_supplier = col_index(mp, "供应商")
        out = []
        for r in range(hi + 1, len(rows)):
            row = rows[r]
            model = row[c_model] if c_model is not None and c_model < len(row) else ""
            if not cell_str(model):
                continue
            # 逐行供应商：仅当用户未在界面手动指定时生效
            row_supplier = None
            if not supplier_override and c_supplier is not None and c_supplier < len(row):
                sv = cell_str(row[c_supplier])
                if sv:
                    row_supplier = SUPPLIER_KEYS.get(sv, sv)
            out.append(build(model,
                             row[c_mat] if c_mat is not None and c_mat < len(row) else "",
                             row[c_size] if c_size is not None and c_size < len(row) else "",
                             row[c_price] if c_price is not None and c_price < len(row) else "",
                             row[c_period] if c_period is not None and c_period < len(row) else "", r,
                             row_supplier=row_supplier))
        return out, {"supplier": supplier, "sheet": sheet, "header_row": hi + 1, "format": "标准表"}

    # 云锦：产品名称 / 规格名称 / 材质 / 单价
    hi, mp = find_header(rows, ["产品名称", "单价"])
    if hi is not None:
        c_model = col_index(mp, "产品名称", "型号")
        c_price = col_index(mp, "单价")
        c_mat = col_index(mp, "材质")
        c_size = col_index(mp, "规格")
        c_period = col_index(mp, "日期", "月份")
        out = []
        for r in range(hi + 1, len(rows)):
            row = rows[r]
            model = row[c_model] if c_model is not None and c_model < len(row) else ""
            if not cell_str(model):
                continue
            out.append(build(model,
                             row[c_mat] if c_mat is not None and c_mat < len(row) else "",
                             row[c_size] if c_size is not None and c_size < len(row) else "",
                             row[c_price] if c_price is not None and c_price < len(row) else "",
                             row[c_period] if c_period is not None and c_period < len(row) else "", r))
        return out, {"supplier": supplier, "sheet": sheet, "header_row": hi + 1, "format": "云锦表"}

    # 新新精密：名称/规格（型号+材质+尺寸混排）+ 单价
    hi, mp = find_header(rows, ["名称", "规格"])
    if hi is not None:
        c_name = col_index(mp, "名称", "规格")
        c_price = col_index(mp, "单价")
        c_period = col_index(mp, "日期", "月份")
        out = []
        for r in range(hi + 1, len(rows)):
            row = rows[r]
            name = row[c_name] if c_name is not None and c_name < len(row) else ""
            if not cell_str(name):
                continue
            model, material, size = split_xinxi_name(name)
            out.append(build(model, material, size,
                             row[c_price] if c_price is not None and c_price < len(row) else "",
                             row[c_period] if c_period is not None and c_period < len(row) else "", r))
        return out, {"supplier": supplier, "sheet": sheet, "header_row": hi + 1, "format": "新新精密表"}

    # 杰盛：型号（可能叫序号列里的尺寸）+ 单价，宽松匹配
    hi, mp = find_header(rows, ["型号"])
    if hi is not None:
        c_model = col_index(mp, "型号")
        c_price = col_index(mp, "单价", "金额")
        c_mat = col_index(mp, "材质")
        c_size = col_index(mp, "序号", "尺寸", "规格")
        c_period = col_index(mp, "日期", "月份")
        out = []
        for r in range(hi + 1, len(rows)):
            row = rows[r]
            model = row[c_model] if c_model is not None and c_model < len(row) else ""
            if not cell_str(model):
                continue
            out.append(build(model,
                             row[c_mat] if c_mat is not None and c_mat < len(row) else "",
                             row[c_size] if c_size is not None and c_size < len(row) else "",
                             row[c_price] if c_price is not None and c_price < len(row) else "",
                             row[c_period] if c_period is not None and c_period < len(row) else "", r))
        return out, {"supplier": supplier, "sheet": sheet, "header_row": hi + 1, "format": "杰盛表"}

    raise ValueError("无法识别表结构：未找到型号/单价等表头字段")


def normalize_model(s):
    """型号标准化：去空格、统一大小写、去首尾符号。"""
    s = cell_str(s).upper()
    s = re.sub(r"\s+", "", s)
    return s.strip("-—_")


def summarize(rows):
    valid = sum(1 for r in rows if not r.get("exclude_reason") and r.get("unit_price"))
    excluded = sum(1 for r in rows if r.get("exclude_reason"))
    return {"total": len(rows), "valid": valid, "excluded": excluded}
