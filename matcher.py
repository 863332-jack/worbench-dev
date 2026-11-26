# -*- coding: utf-8 -*-
"""匹配引擎（智能对账）：按「供应商+型号」匹配历史最低单价，判定状态、量化差异。

三层匹配：
1. 精确层：`model_norm` 归一化后全等，走价格基准索引（默认口径）；
2. 近似层：精确未命中时，忽略 `- _ * 空格` 等符号差异再试一次（`parsers.model_key`），
   命中只作参考，状态强制为「需人工复核」，绝不自动判定为最低价；
3. 未命中：标「无历史记录」。

差异口径（本次新增，见 config.DIFF_*）：
- 单价差异 price_gap = 本单单价 − 历史最低价；差异率 gap_rate = 差额 / 最低价；
- 金额差异 amount_gap = 单价差异 × 数量（数量缺失则为空）；
- 日期差异：账单月 vs 历史最低价所在月 —— 账单月早于基准月（逻辑可疑）、
  间隔超过 BASE_STALE_MONTHS（基准过期）、月份缺失（无法判断时效）。
"""
import json
from config import (
    STATUS_MATCHED_LOWEST, STATUS_NEEDS_REVIEW, STATUS_ABOVE_LOWEST,
    STATUS_NO_HISTORY, STATUS_PARSE_ERROR,
    DIFF_AMT_HIGH, DIFF_AMT_LOW, DIFF_NO_HIST, DIFF_PARSE_ERR, DIFF_MULTI_LEVEL,
    DIFF_DATE_BEFORE, DIFF_BASE_STALE, DIFF_DATE_MISSING, DIFF_NEAR_MODEL,
    BASE_STALE_MONTHS,
)
from parsers import model_key
import db

EPS = 0.0001


def _fmt_price(v):
    return ("%g" % v) if v is not None else "—"


def month_index(period):
    """'YYYY-MM' → 连续月序号；非法/空返回 None。"""
    s = str(period or "").strip()
    if len(s) < 7 or s[4] != "-":
        return None
    try:
        y, m = int(s[:4]), int(s[5:7])
    except ValueError:
        return None
    if not (1 <= m <= 12):
        return None
    return y * 12 + (m - 1)


def lowest_period(detail, lowest):
    """历史最低价对应的月份：取「等于最低价」的记录中最晚的一个。

    取最晚而非最早，是为了判断「这个低价最近是否还成立」——若最近一次出现已很久以前，
    说明基准可能过期。
    """
    if lowest is None or not detail:
        return ""
    periods = [d.get("period") for d in detail
               if d.get("period") and d.get("price") is not None
               and abs(float(d["price"]) - float(lowest)) < EPS]
    return max(periods) if periods else ""


def _date_diffs(bill_period, hist_period):
    """日期类差异，返回 (codes, date_gap, 说明片段)。"""
    bi, hi = month_index(bill_period), month_index(hist_period)
    if bi is None:
        return [DIFF_DATE_MISSING], None, "本单月份缺失，无法判断与基准的时间关系"
    if hi is None:
        return [], None, "历史基准未记录月份，无法判断时效"
    gap = bi - hi          # 正数=基准更早（正常）；负数=账单早于基准（可疑）
    if gap < 0:
        return ([DIFF_DATE_BEFORE], gap,
                "本单月份（%s）早于历史最低价出现月份（%s），相当于用后来的价格倒推" % (bill_period, hist_period))
    if gap >= BASE_STALE_MONTHS:
        return ([DIFF_BASE_STALE], gap,
                "历史最低价来自 %s，距本单 %s 已 %d 个月，价格可能失效" % (hist_period, bill_period, gap))
    return [], gap, ""


def match_one(row, idx_map, key_index=None):
    """对单条待核对记录匹配，返回结果 dict（含 status/evidence/reason/差异字段）。

    idx_map: {(supplier, model_norm): 基准 dict}；key_index: {model_key: [基准 dict]}（近似层，可空）。
    """
    quantity = row.get("quantity")
    base = {
        "supplier": row.get("supplier", ""),
        "model": row.get("model", ""),
        "model_norm": row.get("model_norm", ""),
        "material": row.get("material", ""),
        "size": row.get("size", ""),
        "bill_price": row.get("unit_price"),
        "period": row.get("period", ""),
        "bill_date": row.get("date", "") or "",
        "quantity": quantity,
        "bill_amount": row.get("total_amount"),
        "source_file": row.get("source_file", ""),
        "source_row": row.get("source_row"),
        "raw_values": row.get("raw_values", []),
        "created_at": db.now(),
        # 差异字段统一先占位，保证入库/导出字段齐全
        "lowest_price": None, "price_gap": None, "amount_gap": None, "gap_rate": None,
        "diff_types": "", "hist_period": "", "date_gap": None,
        "evidence": "", "reason": "", "status": "",
    }
    base["bill_date"] = base["bill_date"] or row.get("bill_date", "") or ""
    ctx = {"near": ""}      # 近似匹配说明（一旦命中，任何分支都要带上「型号近似匹配」标记）

    def finish(status, codes, reason, extra_note=""):
        base["status"] = status
        merged = [c for c in codes if c]
        if ctx["near"]:
            merged.append(DIFF_NEAR_MODEL)
        base["diff_types"] = ",".join(dict.fromkeys(merged))
        if extra_note:
            base["reason"] = (reason + "；" + extra_note) if reason else extra_note
        else:
            base["reason"] = reason
        return base

    # 解析失败
    if row.get("exclude_reason") or not row.get("unit_price"):
        base["evidence"] = ""
        return finish(STATUS_PARSE_ERROR, [DIFF_PARSE_ERR],
                      "本行单价无法解析：" + (row.get("exclude_reason") or "空价"))

    idx = idx_map.get((base["supplier"], base["model_norm"]))
    near_note = ""
    if not idx and key_index:
        cands = [c for c in key_index.get(model_key(base["model"]), [])
                 if c.get("supplier") == base["supplier"]
                 and c.get("model_norm") != base["model_norm"]]
        if cands:
            idx = cands[0]
            ctx["near"] = near_note = ("型号近似匹配：本单「%s」≈ 历史「%s」（忽略符号/空格差异后一致）"
                                       % (base["model"], idx.get("model", "")))
            if len(cands) > 1:
                ctx["near"] = near_note = near_note + "；另有 %d 个近似型号（%s）" % (
                    len(cands) - 1, "、".join(c.get("model", "") for c in cands[1:4]))

    # 无历史
    if not idx:
        return finish(STATUS_NO_HISTORY, [DIFF_NO_HIST],
                      "历史价格库中无此型号（供应商+型号）记录，需人工补录或指定基准价")

    lowest = idx.get("min_price")
    bill = base["bill_price"]
    detail = json.loads(idx.get("price_detail") or "[]")
    hist_period = lowest_period(detail, lowest)
    base["lowest_price"] = lowest
    base["hist_period"] = hist_period

    # 差异量化（无最低价时全部留空）
    if lowest is not None and bill is not None:
        gap = bill - lowest
        if abs(gap) < EPS:
            gap = 0.0
        base["price_gap"] = round(gap, 4)
        base["gap_rate"] = round(gap / lowest, 6) if lowest else None
        if quantity not in (None, ""):
            try:
                base["amount_gap"] = round(gap * float(quantity), 2)
            except (TypeError, ValueError):
                base["amount_gap"] = None

    date_codes, date_gap, date_note = _date_diffs(base["period"], hist_period)
    base["date_gap"] = date_gap

    # 依据说明
    ev_parts = ["历史记录 %d 条" % len(detail)]
    if detail:
        ev_parts.append("历史单价：" + "、".join(_fmt_price(d.get("price")) for d in detail))
    ev_parts.append("历史最低价 %s" % _fmt_price(lowest))
    if hist_period:
        ev_parts.append("最低价期间 %s" % hist_period)
    if idx.get("materials"):
        ev_parts.append("材质(展示)：" + idx["materials"])
    if base["price_gap"] is not None and base["price_gap"] != 0:
        ev_parts.append("单价差异 %s（%s%.2f%%）" % (
            ("%+.4g" % base["price_gap"]), "+" if base["price_gap"] > 0 else "",
            (base["gap_rate"] or 0) * 100))
    if base["amount_gap"] is not None and base["amount_gap"] != 0:
        ev_parts.append("金额差异 %+.2f" % base["amount_gap"])
    base["evidence"] = "；".join(ev_parts)

    codes = list(date_codes)
    if date_note:
        codes_note = date_note
    else:
        codes_note = ""

    # 同型号多档价 → 需人工复核（先于比价）
    if idx.get("risk_flag"):
        levels = sorted(set(round(d["price"], 4) for d in detail if d.get("price") is not None))
        return finish(STATUS_NEEDS_REVIEW, codes + [DIFF_MULTI_LEVEL],
                      "该型号历史存在 %d 档单价（%s），可能对应不同规格；最低价 %s 是否适用需人工确认"
                      % (len(levels), "、".join(_fmt_price(x) for x in levels), _fmt_price(lowest)),
                      "；".join(x for x in (near_note, codes_note) if x))

    # 近似匹配：无论价格是否相等，一律转人工确认
    if near_note:
        return finish(STATUS_NEEDS_REVIEW, codes + [DIFF_NEAR_MODEL],
                      "型号未精确命中，已按近似型号比价，需确认是否同一物料",
                      "；".join(x for x in (near_note, codes_note) if x))

    # 已匹配最低价
    if bill is not None and lowest is not None and abs(bill - lowest) < EPS:
        return finish(STATUS_MATCHED_LOWEST, codes, "", codes_note)

    # 低于历史最低价
    if bill is not None and lowest is not None and bill < lowest:
        return finish(STATUS_NEEDS_REVIEW, codes + [DIFF_AMT_LOW],
                      "本单单价 %s 低于历史最低价 %s，属新高价或录入异常，需人工确认"
                      % (_fmt_price(bill), _fmt_price(lowest)), codes_note)

    # 高于历史最低价
    return finish(STATUS_ABOVE_LOWEST, codes + [DIFF_AMT_HIGH],
                  "本单单价 %s 高于历史最低价 %s，未匹配最低价，请核对"
                  % (_fmt_price(bill), _fmt_price(lowest)), codes_note)


def run_match(rows):
    """批量匹配，返回 (结果列表, 统计dict)。价格基准与近似索引一次性预载。"""
    idx_map = db.load_price_index()
    key_index = db.load_model_key_index()
    results = [match_one(r, idx_map, key_index) for r in rows]
    counts = {
        "total": len(results),
        "matched": sum(1 for r in results if r["status"] == STATUS_MATCHED_LOWEST),
        "review": sum(1 for r in results if r["status"] in (STATUS_NEEDS_REVIEW,)),
        "above": sum(1 for r in results if r["status"] == STATUS_ABOVE_LOWEST),
        "nohistory": sum(1 for r in results if r["status"] == STATUS_NO_HISTORY),
        "error": sum(1 for r in results if r["status"] == STATUS_PARSE_ERROR),
        # 智能对账统计：有金额差异的记录数 / 超付合计 / 少付合计
        "diff": sum(1 for r in results
                    if r.get("amount_gap") is not None and abs(r["amount_gap"]) > 0.0001),
        "overpay": round(sum(r["amount_gap"] for r in results
                             if r.get("amount_gap") and r["amount_gap"] > 0), 2),
        "underpay": round(sum(r["amount_gap"] for r in results
                              if r.get("amount_gap") and r["amount_gap"] < 0), 2),
    }
    return results, counts
