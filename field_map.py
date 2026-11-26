# -*- coding: utf-8 -*-
"""字段映射：系统内部字段 <-> 用户上传账单表头。

核对明细必须以用户上传账单的表头为准：字段名称、列顺序、列数量完全一致。
本模块负责：
1. 系统标准字段 -> 上传表头列的映射；
2. 无法映射的列 / 系统字段缺失的差异提示；
3. 按表头生成输出行（值取原始单元格，原样保留）。
"""
import re

# 系统内部字段 -> 标准表头（规范化名）。用于把内部值放回对应列。
SYSTEM_TO_HEADER = {
    "supplier": "供应商",
    "date": "日期",
    "model": "型号",
    "size": "尺寸",
    "material": "材质",
    "quantity": "数量",
    "unit_price": "单价",
    "total_amount": "合计金额",
    "remark": "备注",
}

# 核对派生字段（系统计算，不属于账单，不进入按表头输出的明细）
DERIVED_FIELDS = {
    "status": "状态", "lowest_price": "历史最低价", "evidence": "依据",
    "reason": "复核原因", "source_file": "来源文件", "source_row": "行号",
}


def norm(s):
    """表头规范化：去空格、星号、全角符号，用于匹配但不改变原文。"""
    return re.sub(r"[\s*＊（）()【】\[\]]", "", str(s or "")).strip()


def build_mapping(headers):
    """根据上传表头建立映射。

    返回 dict:
      col_to_field: {列序号(0based): 系统字段名或 None}
      field_to_col: {系统字段名: 列序号}
      unmapped_cols: [(列序号, 原表头名)] 无法映射到系统字段的列
      missing_fields: [系统字段名] 标准字段中在表头里缺失的
      header_norm: [规范化表头名]
    """
    header_norm = [norm(h) for h in headers]
    col_to_field = {}
    field_to_col = {}
    unmapped_cols = []

    # 反向：规范化表头 -> 系统字段
    norm_to_field = {norm(v): k for k, v in SYSTEM_TO_HEADER.items()}

    for idx, hn in enumerate(header_norm):
        field = norm_to_field.get(hn)
        if field is None:
            # 容错：常见别名
            alias = {
                "单价元": "unit_price", "价格": "unit_price",
                "型号规格": "model", "产品名称": "model",
                "金额": "total_amount", "合计": "total_amount",
                "数量件": "quantity", "日期时间": "date",
            }
            field = alias.get(hn)
        col_to_field[idx] = field
        if field:
            field_to_col.setdefault(field, idx)
        else:
            unmapped_cols.append((idx, headers[idx]))

    missing_fields = [f for f in SYSTEM_TO_HEADER if f not in field_to_col]
    return {
        "col_to_field": col_to_field,
        "field_to_col": field_to_col,
        "unmapped_cols": unmapped_cols,
        "missing_fields": missing_fields,
        "header_norm": header_norm,
    }


def describe_diff(mapping, headers):
    """生成人类可读的字段映射差异说明列表。"""
    diffs = []
    for idx, name in mapping["unmapped_cols"]:
        diffs.append("第%d列「%s」无法映射到系统字段，将按原表头原样输出该列值"
                     % (idx + 1, name))
    for f in mapping["missing_fields"]:
        diffs.append("系统字段「%s」（对应标准列「%s」）在上传表头中不存在，未新增该列"
                     % (f, SYSTEM_TO_HEADER[f]))
    return diffs


def row_by_header(raw_values, headers, mapping):
    """按上传表头输出行值：原样使用原始单元格值，不增删改。

    raw_values 长度不足时补空；多余时截断到表头列数。
    """
    n = len(headers)
    vals = list(raw_values or [])[:n]
    while len(vals) < n:
        vals.append("")
    return vals
