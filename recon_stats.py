# -*- coding: utf-8 -*-
"""智能对账统计：把匹配结果汇总成「按状态 / 按供应商 / 按差异类型」的统计口径。

口径约定（与页面、Excel 完全一致，避免两处各算一套）：
- 本单金额 bill_total  = Σ 本单合计金额（上传账单「合计金额」列）
- 应有金额 should_total = Σ 历史最低价 × 数量（数量缺失的行不计入）
- 超付 overpay         = Σ 正金额差异（本单高于最低价的部分）
- 少付 underpay        = Σ 负金额差异
- 差异笔数 diff_rows   = 金额差异不为 0 的行数（金额无法换算时退回按单价差异判定）
"""
from config import (
    STATUS_LABELS, STATUS_MATCHED_LOWEST, STATUS_NEEDS_REVIEW, STATUS_ABOVE_LOWEST,
    STATUS_NO_HISTORY, STATUS_PARSE_ERROR, DIFF_LABELS,
)

EPS = 0.0001
STATUS_ORDER = (STATUS_MATCHED_LOWEST, STATUS_NEEDS_REVIEW, STATUS_ABOVE_LOWEST,
                STATUS_NO_HISTORY, STATUS_PARSE_ERROR)


def _f(v):
    """数值归一：None / 空串 → None，其它转 float。"""
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def diff_codes(row):
    return [c for c in str(row.get("diff_types") or "").split(",") if c]


def _row_gap(row):
    """金额差异：优先 amount_gap；无数量时退回单价差异（并按 1 计）。"""
    amt = _f(row.get("amount_gap"))
    if amt is not None:
        return amt
    return _f(row.get("price_gap"))


def summarize(results):
    """返回统计 dict（供 /api/results 与 Excel「差异汇总」共用）。"""
    status_counts = {s: 0 for s in STATUS_ORDER}
    bill_total = should_total = overpay = underpay = 0.0
    diff_rows = amount_known = 0
    suppliers = {}

    for r in results:
        st = r.get("status")
        status_counts[st] = status_counts.get(st, 0) + 1

        qty = _f(r.get("quantity"))
        bill_amt = _f(r.get("bill_amount"))
        lowest = _f(r.get("lowest_price"))
        gap = _row_gap(r)

        if bill_amt is not None:
            bill_total += bill_amt
        elif lowest is not None and qty is not None:
            bp = _f(r.get("bill_price"))
            if bp is not None:
                bill_total += bp * qty
        if lowest is not None and qty is not None:
            should_total += lowest * qty
            amount_known += 1

        if gap is not None and abs(gap) > EPS:
            diff_rows += 1
            if gap > 0:
                overpay += gap
            else:
                underpay += gap

        sup = r.get("supplier") or "(未填)"
        s = suppliers.setdefault(sup, {
            "supplier": sup, "total": 0, "matched": 0, "above": 0, "review": 0,
            "nohistory": 0, "error": 0, "diff_rows": 0, "overpay": 0.0,
            "underpay": 0.0, "max_gap_rate": 0.0,
        })
        s["total"] += 1
        key = {STATUS_MATCHED_LOWEST: "matched", STATUS_ABOVE_LOWEST: "above",
               STATUS_NEEDS_REVIEW: "review", STATUS_NO_HISTORY: "nohistory",
               STATUS_PARSE_ERROR: "error"}.get(st)
        if key:
            s[key] += 1
        if gap is not None and abs(gap) > EPS:
            s["diff_rows"] += 1
            if gap > 0:
                s["overpay"] += gap
            else:
                s["underpay"] += gap
            rate = _f(r.get("gap_rate"))
            if rate is not None and abs(rate) > abs(s["max_gap_rate"]):
                s["max_gap_rate"] = rate

    by_diff = {}
    for r in results:
        for code in diff_codes(r):
            d = by_diff.setdefault(code, {"code": code, "label": DIFF_LABELS.get(code, code),
                                          "rows": 0, "overpay": 0.0, "underpay": 0.0})
            d["rows"] += 1
            gap = _row_gap(r)
            if gap is not None:
                if gap > 0:
                    d["overpay"] += gap
                elif gap < 0:
                    d["underpay"] += gap

    for s in suppliers.values():
        s["overpay"] = round(s["overpay"], 2)
        s["underpay"] = round(s["underpay"], 2)
        s["max_gap_rate"] = round(s["max_gap_rate"], 6)
    for d in by_diff.values():
        d["overpay"] = round(d["overpay"], 2)
        d["underpay"] = round(d["underpay"], 2)

    return {
        "total": len(results),
        "by_status": [{"code": s, "label": STATUS_LABELS.get(s, s), "rows": status_counts.get(s, 0)}
                      for s in STATUS_ORDER],
        "amount": {
            "bill_total": round(bill_total, 2),
            "should_total": round(should_total, 2),
            "overpay": round(overpay, 2),
            "underpay": round(underpay, 2),
            "net_gap": round(overpay + underpay, 2),
            "diff_rows": diff_rows,
            "rows_with_quantity": amount_known,
        },
        "by_supplier": sorted(suppliers.values(), key=lambda x: -x["overpay"]),
        "by_diff": sorted(by_diff.values(), key=lambda x: (-x["rows"], x["code"])),
    }
