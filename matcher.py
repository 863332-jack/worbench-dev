# -*- coding: utf-8 -*-
"""匹配引擎：按「供应商+型号」匹配历史最低单价，判定状态与依据。

本次重构（缺陷 P3-18）：run_match 开始时全量预载价格基准（db.load_price_index），
逐行匹配改为内存 dict 查找，不再每行新建数据库连接。
"""
import json
from config import (
    STATUS_MATCHED_LOWEST, STATUS_NEEDS_REVIEW, STATUS_ABOVE_LOWEST,
    STATUS_NO_HISTORY, STATUS_PARSE_ERROR,
)
import db


def _fmt_price(v):
    return ("%g" % v) if v is not None else "—"


def match_one(row, idx_map):
    """对单条待核对记录匹配，返回结果 dict（含 status/evidence/reason）。

    idx_map: {(supplier, model_norm): 基准 dict}，由 run_match 预载。
    """
    base = {
        "supplier": row.get("supplier", ""),
        "model": row.get("model", ""),
        "model_norm": row.get("model_norm", ""),
        "material": row.get("material", ""),
        "size": row.get("size", ""),
        "bill_price": row.get("unit_price"),
        "period": row.get("period", ""),
        "source_file": row.get("source_file", ""),
        "source_row": row.get("source_row"),
        "raw_values": row.get("raw_values", []),
        "created_at": db.now(),
    }
    # 解析失败
    if row.get("exclude_reason") or not row.get("unit_price"):
        base.update(status=STATUS_PARSE_ERROR, lowest_price=None,
                    evidence="", reason="本行单价无法解析：" + (row.get("exclude_reason") or "空价"))
        return base

    idx = idx_map.get((base["supplier"], base["model_norm"]))
    # 无历史
    if not idx:
        base.update(status=STATUS_NO_HISTORY, lowest_price=None, evidence="",
                    reason="历史价格库中无此型号（供应商+型号）记录，需人工补录或指定基准价")
        return base

    lowest = idx["min_price"]
    bill = base["bill_price"]
    detail = json.loads(idx["price_detail"] or "[]")
    ev_parts = ["历史记录 %d 条" % len(detail)]
    if detail:
        prices = [_fmt_price(d["price"]) for d in detail]
        ev_parts.append("历史单价：" + "、".join(prices))
    ev_parts.append("历史最低价 %s" % _fmt_price(lowest))
    if idx["materials"]:
        ev_parts.append("材质(展示)：" + idx["materials"])
    evidence = "；".join(ev_parts)

    # 同型号多档价 → 需人工复核（先于比价）
    if idx["risk_flag"]:
        levels = sorted(set(round(d["price"], 4) for d in detail if d.get("price") is not None))
        base.update(status=STATUS_NEEDS_REVIEW, lowest_price=lowest, evidence=evidence,
                    reason="该型号历史存在 %d 档单价（%s），可能对应不同规格；最低价 %s 是否适用需人工确认"
                           % (len(levels), "、".join(_fmt_price(x) for x in levels), _fmt_price(lowest)))
        return base

    # 已匹配最低价
    if bill is not None and lowest is not None and abs(bill - lowest) < 0.0001:
        base.update(status=STATUS_MATCHED_LOWEST, lowest_price=lowest, evidence=evidence, reason="")
        return base

    # 低于历史最低价
    if bill is not None and lowest is not None and bill < lowest:
        base.update(status=STATUS_NEEDS_REVIEW, lowest_price=lowest, evidence=evidence,
                    reason="本单单价 %s 低于历史最低价 %s，属新高价或录入异常，需人工确认"
                           % (_fmt_price(bill), _fmt_price(lowest)))
        return base

    # 高于历史最低价
    base.update(status=STATUS_ABOVE_LOWEST, lowest_price=lowest, evidence=evidence,
                reason="本单单价 %s 高于历史最低价 %s，未匹配最低价，请核对" % (_fmt_price(bill), _fmt_price(lowest)))
    return base


def run_match(rows):
    """批量匹配，返回 (结果列表, 统计dict)。价格基准一次性预载。"""
    idx_map = db.load_price_index()
    results = [match_one(r, idx_map) for r in rows]
    counts = {
        "total": len(results),
        "matched": sum(1 for r in results if r["status"] == STATUS_MATCHED_LOWEST),
        "review": sum(1 for r in results if r["status"] in (STATUS_NEEDS_REVIEW,)),
        "above": sum(1 for r in results if r["status"] == STATUS_ABOVE_LOWEST),
        "nohistory": sum(1 for r in results if r["status"] == STATUS_NO_HISTORY),
        "error": sum(1 for r in results if r["status"] == STATUS_PARSE_ERROR),
    }
    return results, counts
