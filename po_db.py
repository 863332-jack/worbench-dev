# -*- coding: utf-8 -*-
"""采购订单流程数据层。

设计要点：
- 三张表：po_order（订单主表）/ po_event（状态轨迹）/ po_user（账号与角色）；
- 状态机：pending_confirm（待确认）→ confirmed（已接单）→ received（已收货）；
  待确认状态可被供应商 rejected（已拒接，终止流程）；
- 所有状态流转使用「带前置状态条件的原子 UPDATE」，并发下不会重复推进；
- 角色：purchaser（采购方，下单/查看全部/登记收货）、supplier（供应商，仅本家订单/确认）；
- 账号密码使用 werkzeug pbkdf2 哈希存储，种子账号首次启动自动创建。
"""
import os
import sqlite3
from datetime import datetime

from werkzeug.security import generate_password_hash, check_password_hash

import db
import parsers

# 角色
ROLE_PURCHASER = "purchaser"
ROLE_SUPPLIER = "supplier"

# 订单状态
STATUS_PENDING = "pending_confirm"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"
STATUS_RECEIVED = "received"

STATUS_LABELS = {
    STATUS_PENDING: "待确认",
    STATUS_CONFIRMED: "已接单",
    STATUS_REJECTED: "已拒接",
    STATUS_RECEIVED: "已收货",
}

# 状态流转图（供前端展示与校验提示）
STATUS_FLOW = [
    (STATUS_PENDING, STATUS_CONFIRMED, "供应商确认接单"),
    (STATUS_CONFIRMED, STATUS_RECEIVED, "采购方登记收货"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS po_order (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT UNIQUE NOT NULL,
    supplier TEXT NOT NULL,
    purchaser TEXT NOT NULL,          -- 下单采购员
    product_name TEXT NOT NULL,
    model TEXT,
    quantity REAL NOT NULL,
    unit TEXT,
    unit_price REAL,
    total_amount REAL,
    required_date TEXT,               -- 期望交期
    remark TEXT,
    drawing_name TEXT,                -- 图纸原始文件名
    drawing_path TEXT,                -- 相对路径（uploads/po/<order_no>/xxx）
    status TEXT NOT NULL DEFAULT 'pending_confirm',
    created_at TEXT,
    confirmed_at TEXT,
    confirmed_by TEXT,
    reject_reason TEXT,
    received_at TEXT,
    received_qty REAL,
    received_date TEXT,
    receive_remark TEXT,
    receipt_name TEXT,
    receipt_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_po_status ON po_order(status);
CREATE INDEX IF NOT EXISTS idx_po_supplier ON po_order(supplier, status);

CREATE TABLE IF NOT EXISTS po_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT NOT NULL,
    action TEXT NOT NULL,             -- create / confirm / reject / receive
    actor TEXT,
    role TEXT,
    detail TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_po_event_order ON po_event(order_no, id);

CREATE TABLE IF NOT EXISTS po_user (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,               -- purchaser / supplier
    supplier TEXT,                    -- supplier 角色绑定的供应商
    display_name TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS po_order_item (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT NOT NULL,
    line_no INTEGER NOT NULL,         -- 行号（1 起）
    model TEXT,                       -- 型号（匹配键）
    param TEXT,                       -- 参数（历史档价摘要，可改）
    size TEXT,                        -- 尺寸（历史回填，可改）
    material TEXT,                    -- 材料（历史回填，可改）
    quantity REAL,                    -- 下单数量
    unit TEXT,
    unit_price REAL,                  -- 单价（历史最低价回填，可改）
    required_date TEXT,               -- 交货日期
    remark TEXT,                      -- 特殊备注/处理要求
    drawing_name TEXT,                -- 该行图纸原始文件名
    drawing_path TEXT,                -- 该行图纸相对路径
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_po_item_order ON po_order_item(order_no, line_no);
"""

# 单次下单最多产品行数
MAX_LINES = 50

# 种子账号（首次启动创建；密码请首次登录后按需修改 config）
SEED_USERS = [
    ("purchaser", "123456", ROLE_PURCHASER, None, "采购员"),
    ("jiesheng", "123456", ROLE_SUPPLIER, "杰盛", "杰盛（供应商）"),
    ("xinxin", "123456", ROLE_SUPPLIER, "新新精密", "新新精密（供应商）"),
    ("yunjin", "123456", ROLE_SUPPLIER, "云锦", "云锦（供应商）"),
]

# 图纸/凭证允许的扩展名（单一来源：后端校验与前端 accept 都取自这里）
# 按用途分组，覆盖工程图纸 / 3D 模型 / 图片 / Office / 压缩包 / 文本
DOC_EXTS = (
    # 工程图纸与 3D 模型
    ".pdf", ".dwg", ".dxf", ".dwf", ".step", ".stp", ".igs", ".iges",
    ".x_t", ".x_b", ".sldprt", ".sldasm", ".prt", ".catpart", ".3dxml",
    # 图片（含扫描件/传真件常见格式）
    ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff", ".heic",
    # Office / WPS 文档
    ".xlsx", ".xlsm", ".xls", ".csv", ".doc", ".docx", ".ppt", ".pptx", ".wps", ".et",
    # 压缩包（成套图纸常用）
    ".zip", ".rar", ".7z",
    # 纯文本 / 通用交换
    ".txt", ".md", ".xml", ".json",
)

# 前端 <input accept> 用（MIME 组合在部分浏览器里会误伤扩展名，这里直接用扩展名列表）
DOC_ACCEPT = ",".join(DOC_EXTS)

# 系统内置的供应商（历史库中若出现其它供应商，会自动并入可选列表）
KNOWN_SUPPLIERS = ("杰盛", "新新精密", "云锦")


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init():
    conn = db.connect()
    conn.executescript(SCHEMA)
    n = conn.execute("SELECT COUNT(*) FROM po_user").fetchone()[0]
    if n == 0:
        for username, pwd, role, supplier, disp in SEED_USERS:
            conn.execute(
                "INSERT INTO po_user(username,password_hash,role,supplier,display_name,created_at) VALUES(?,?,?,?,?,?)",
                (username, generate_password_hash(pwd), role, supplier, disp, now()),
            )
    conn.commit()
    conn.close()


def new_order_no():
    import random
    return "PO" + datetime.now().strftime("%Y%m%d%H%M%S") + str(random.randint(0, 9999)).zfill(4)


# ---------- 账号 ----------

def verify_user(username, password):
    """校验账号密码，成功返回用户 dict，失败返回 None。"""
    conn = db.connect()
    row = conn.execute("SELECT * FROM po_user WHERE username=?", (str(username or "").strip(),)).fetchone()
    conn.close()
    if not row:
        return None
    if not check_password_hash(row["password_hash"], str(password or "")):
        return None
    return {"username": row["username"], "role": row["role"],
            "supplier": row["supplier"], "display_name": row["display_name"]}


# ---------- 订单 ----------

def create_order_multi(supplier, purchaser, remark, items, order_no=None):
    """创建多行订单（1..MAX_LINES 行），单事务写订单头 + 明细行 + create 事件。

    items: [{line_no, model, param, size, material, quantity, unit,
             unit_price, required_date, remark, drawing_name, drawing_path}]
    订单头字段由明细汇总：数量=总数量、金额=Σ(数量×单价)、交期=最早行交期。
    """
    items = items or []
    if not (1 <= len(items) <= MAX_LINES):
        raise ValueError("订单行数必须为 1-%d 行，当前 %d 行" % (MAX_LINES, len(items)))
    order_no = order_no or new_order_no()
    total_qty = sum(float(it.get("quantity") or 0) for it in items)
    total_amt = round(sum(float(it["quantity"]) * float(it["unit_price"])
                          for it in items if it.get("unit_price") is not None), 2)
    dates = [it["required_date"] for it in items if it.get("required_date")]
    earliest = min(dates) if dates else ""
    models = [str(it.get("model") or "") for it in items if it.get("model")]
    title = ("多产品订单（%d 项）" % len(items)) if len(items) > 1 else (models[0] if models else "单产品订单")

    conn = db.connect()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO po_order
            (order_no,supplier,purchaser,product_name,model,quantity,unit,unit_price,total_amount,
             required_date,remark,drawing_name,drawing_path,status,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (order_no, supplier, purchaser, title, (models[0] if models else ""),
             total_qty, "", None, total_amt, earliest, remark or "", "", "", STATUS_PENDING, now()),
        )
        for it in items:
            conn.execute(
                """INSERT INTO po_order_item
                (order_no,line_no,model,param,size,material,quantity,unit,unit_price,
                 required_date,remark,drawing_name,drawing_path,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (order_no, it.get("line_no"), it.get("model", ""), it.get("param", ""),
                 it.get("size", ""), it.get("material", ""), it.get("quantity"),
                 it.get("unit", ""), it.get("unit_price"), it.get("required_date", ""),
                 it.get("remark", ""), it.get("drawing_name", ""),
                 it.get("drawing_path", ""), now()),
            )
        shown = "、".join(models[:5]) + ("等" if len(models) > 5 else "")
        conn.execute(
            "INSERT INTO po_event(order_no,action,actor,role,detail,created_at) VALUES(?,?,?,?,?,?)",
            (order_no, "create", purchaser, ROLE_PURCHASER,
             "提交下单：共 %d 项产品（%s），供应商 %s" % (len(items), shown, supplier), now()),
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return get_order(order_no)


def get_items(order_no):
    """取订单明细行（按行号）。"""
    conn = db.connect()
    rows = conn.execute(
        "SELECT * FROM po_order_item WHERE order_no=? ORDER BY line_no", (order_no,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_suppliers():
    """可选供应商 = 内置供应商 ∪ 历史库实际出现的供应商。

    修复缺陷 P1-6：此前下单页与后端都硬编码 3 家供应商，若历史数据里
    的供应商写法不同（含空格/别名），采购端永远匹配不到 → 显示「未找到」。
    """
    out = list(KNOWN_SUPPLIERS)
    try:
        conn = db.connect()
        rows = conn.execute(
            "SELECT DISTINCT supplier FROM hist_price WHERE supplier IS NOT NULL AND supplier<>''"
        ).fetchall()
        conn.close()
    except Exception:
        return out
    for r in rows:
        s = str(r["supplier"]).strip()
        if s and s not in out:
            out.append(s)
    return out


def _load_supplier_rows(conn, supplier):
    """取某供应商全部历史明细（近似的候选用；仅在精确匹配落空时才调用）。"""
    return conn.execute(
        "SELECT " + _HIST_COLS + " FROM hist_price WHERE supplier=? ORDER BY period DESC, id DESC",
        (supplier,),
    ).fetchall()


# ---------- 型号匹配：候选检索 + 字段级溯源 ----------
# 匹配强度由强到弱：完全一致 → 忽略符号一致 → 前缀相近
MATCH_EXACT = "exact"        # 归一化后完全相同
MATCH_RELATED = "related"    # 忽略分隔符/乘号/直径符号后相同
MATCH_PREFIX = "prefix"      # 前缀或包含关系（相近型号）
MATCH_NONE = "none"

MATCH_LABELS = {
    MATCH_EXACT: "型号完全一致",
    MATCH_RELATED: "忽略符号差异后一致",
    MATCH_PREFIX: "型号前缀相近",
    MATCH_NONE: "无匹配",
}

# 单次返回的候选上限（超出只保留最新若干条）
CANDIDATE_LIMIT = 12

_HIST_COLS = "model, model_norm, size, material, unit_price, period, source_file, source_row, supplier"


def _source_obj(r):
    """把一条历史记录转成「数据来源」对象，供界面标注自动填充出处。"""
    period = str(r.get("period") or "").strip()
    supplier = str(r.get("supplier") or "").strip()
    fname = os.path.basename(str(r.get("source_file") or "").strip())
    row = r.get("source_row")
    parts = [("%s 账单" % period) if period else "历史账单"]
    if supplier:
        parts.append(supplier)
    if fname:
        parts.append(fname + ((" 第 %d 行" % row) if row else ""))
    return {"period": period, "supplier": supplier, "file": fname,
            "row": row, "text": " · ".join(parts)}


def _collect_candidates(conn, supplier, norm, model_raw):
    """分层取候选记录。返回 (候选行列表, 匹配类型, 说明文字)。

    严格按强度取一层，不与更弱的层混合 —— 避免把「不同规格的相近型号」
    混进精确命中的结果里，从源头减少不确定信息。
    """
    exact = conn.execute(
        "SELECT " + _HIST_COLS + " FROM hist_price WHERE supplier=? AND model_norm=? "
        "ORDER BY period DESC, id DESC", (supplier, norm)).fetchall()
    if exact:
        return [dict(r, match_kind=MATCH_EXACT) for r in exact], MATCH_EXACT, ""
    rows = _load_supplier_rows(conn, supplier)
    key = parsers.model_key(model_raw)
    if key:
        rel = [r for r in rows if parsers.model_key(r["model"]) == key]
        if rel:
            return ([dict(r, match_kind=MATCH_RELATED) for r in rel], MATCH_RELATED,
                    "忽略分隔符后一致")
    pre = [r for r in rows if r["model_norm"] and norm
           and (r["model_norm"].startswith(norm) or norm.startswith(r["model_norm"]))]
    if pre:
        return ([dict(r, match_kind=MATCH_PREFIX) for r in pre], MATCH_PREFIX,
                "前缀/包含关系")
    return [], MATCH_NONE, ""


def _resolve_field(cands, field):
    """字段级判定：唯一取值才允许自动填充；出现多个取值即判定为歧义，交给人工选择。

    返回 (值或"", 来源 dict 或 None, 是否歧义, 全部取值列表)。
    这样「同型号不同尺寸/材质」不会随便填一个进去。
    """
    vals = []
    for c in cands:
        v = str(c.get(field) or "").strip()
        if v and v not in vals:
            vals.append(v)
    if len(vals) == 1:
        src = next(c for c in cands if str(c.get(field) or "").strip() == vals[0])
        return vals[0], _source_obj(src), False, vals
    return "", None, len(vals) >= 2, vals


def _fmt_result(cands, match_type, how, supplier, model_raw, total_matches):
    """把候选整理成「回填值 + 候选列表 + 字段来源 + 歧义标记」。

    只把**唯一确定**的字段放进 auto（前端据此自动填充并标注来源），
    多值字段一律留空并要求用户从候选里选，避免填入不确定信息。
    """
    # 去重（同型号同尺寸同材质同价的重复账期只留最新一条），并截断候选数
    uniq, seen = [], set()
    for c in cands:
        k = (c.get("model_norm"), c.get("size"), c.get("material"), c.get("unit_price"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(c)
        if len(uniq) >= CANDIDATE_LIMIT:
            break

    size, size_src, size_amb, size_vals = _resolve_field(uniq, "size")
    material, mat_src, mat_amb, mat_vals = _resolve_field(uniq, "material")

    # 单价：只有唯一档价才自动填充（多档价 = 取决于规格，属不确定信息）
    price_rows = [c for c in uniq if c.get("unit_price") and c["unit_price"] > 0]
    levels = sorted({round(c["unit_price"], 4) for c in price_rows})
    price_val, price_src = None, None
    if len(levels) == 1:
        price_val = levels[0]
        price_src = _source_obj(next(c for c in price_rows
                                     if round(c["unit_price"], 4) == levels[0]))
    price_txt = " / ".join("¥%g" % p for p in levels) if levels else "无有效价"
    param = ("%d 档价 %s" % (len(levels), price_txt)) if levels else ""

    auto, ambiguous = {}, []
    # 只有「型号完全一致」才谈自动填充；相近型号一律不填（等人工从候选中选定），
    # 这是"避免填入不确定信息"的硬边界。
    if match_type == MATCH_EXACT:
        if size:
            auto["size"] = {"value": size, "source": size_src}
        elif size_amb:
            ambiguous.append("size")
        if material:
            auto["material"] = {"value": material, "source": mat_src}
        elif mat_amb:
            ambiguous.append("material")
        if price_val is not None:
            auto["unit_price"] = {"value": price_val, "source": price_src}
        elif len(levels) >= 2:
            ambiguous.append("unit_price")
        if param:
            auto["param"] = {"value": param,
                            "source": {"period": "", "supplier": supplier, "file": "",
                                       "row": None,
                                       "text": "综合自 %d 条历史记录" % len(cands)}}
    else:
        # 相近型号：字段值即使唯一也不填，避免把"另一个零件"的规格套上来
        size = material = ""
        price_val = None
        ambiguous = [f for f, ok in (("size", bool(size_src)), ("material", bool(mat_src)),
                                     ("unit_price", bool(price_src))) if ok]

    matched_model = str(uniq[0].get("model") or "").strip()
    risk = bool(ambiguous) or len(levels) >= 2
    src_text = (size_src or mat_src or price_src or {}).get("text", "")

    if match_type == MATCH_EXACT:
        if ambiguous:
            what = []
            if size_amb:
                what.append("尺寸（%s）" % " / ".join(size_vals))
            if mat_amb:
                what.append("材质（%s）" % " / ".join(mat_vals))
            if "unit_price" in ambiguous:
                what.append("单价（%s）" % price_txt)
            msg = ("型号与历史账单完全一致，但同一型号存在 %d 条不同记录，%s 不唯一："
                   "已列出候选，请选择后再填充，未自动填入以免填错"
                   % (len(uniq), "、".join(what)))
        else:
            msg = "已按历史账单自动填充尺寸 / 材质 / 单价（来源：%s）" % src_text
    else:
        msg = ("型号「%s」未与历史账单完全一致，按%s匹配到 %d 条相近记录（如「%s」）："
               "已列出候选，请选择其中一条后填充，不自动填入不确定信息"
               % (model_raw, how or MATCH_LABELS.get(match_type, "近似"), len(uniq), matched_model))

    suggestions = []
    for c in uniq:
        m = str(c.get("model") or "").strip()
        if m and m not in suggestions:
            suggestions.append(m)

    return {
        "found": True, "match_type": match_type, "match_label": MATCH_LABELS.get(match_type, ""),
        "matches": total_matches, "risk": risk,
        "size": size, "material": material, "param": param, "unit_price": price_val,
        "matched_model": matched_model,
        "suggestions": suggestions[:5],
        "supplier_hint": "",
        "auto": auto,
        "ambiguous": ambiguous,
        "ambiguous_values": {"size": size_vals, "material": mat_vals, "unit_price": levels},
        "candidates": [_cand_public(c, match_type) for c in uniq],
        "message": msg,
    }


def _cand_public(c, match_type):
    """候选行的对外结构（含来源标注）。"""
    return {
        "model": str(c.get("model") or "").strip(),
        "model_norm": c.get("model_norm") or "",
        "size": str(c.get("size") or "").strip(),
        "material": str(c.get("material") or "").strip(),
        "unit_price": c.get("unit_price"),
        "period": c.get("period") or "",
        "match_kind": c.get("match_kind") or match_type,
        "source": _source_obj(c),
    }


def lookup_model(supplier, model_raw):
    """按「供应商 + 型号（归一化）」从历史账单检索，返回候选列表与可安全回填的字段。

    数据源：历史账单（hist_price，即入口 ① 导入的账单明细）。
    流程：分层检索候选 → 字段级判定唯一性 → 只把唯一值放进 auto（附来源）
    → 多值/近似一律交给用户从候选里选。

    返回 dict:
      found / match_type(exact|related|prefix|none) / match_label / matches / risk
      auto: {字段: {value, source}}  可自动填充的字段（size/material/unit_price/param）
      ambiguous: ["size","material","unit_price"]  取值不唯一、必须人工选择的字段
      candidates: 候选列表（型号/尺寸/材质/单价/账期/来源行）
      size/material/unit_price/param: 等价于 auto 中的值（兼容旧调用方）
      matched_model / suggestions / supplier_hint / message
    """
    norm = parsers.normalize_model(model_raw or "")
    blank = {"found": False, "match_type": MATCH_NONE, "match_label": MATCH_LABELS[MATCH_NONE],
             "matches": 0, "risk": False, "size": "", "material": "", "param": "",
             "unit_price": None, "matched_model": "", "suggestions": [], "supplier_hint": "",
             "auto": {}, "ambiguous": [], "ambiguous_values": {}, "candidates": []}
    if not norm:
        return dict(blank, message="请输入型号")
    conn = db.connect()
    try:
        cands, match_type, how = _collect_candidates(conn, supplier, norm, model_raw)
        if cands:
            return _fmt_result(cands, match_type, how, supplier, model_raw, len(cands))

        # 未命中：做跨供应商诊断，给出可操作提示（不猜、不乱填）
        hint, sugg = "", []
        try:
            others = conn.execute(
                "SELECT DISTINCT supplier FROM hist_price WHERE model_norm=?", (norm,)
            ).fetchall()
            names = [str(r["supplier"]) for r in others if r["supplier"] and r["supplier"] != supplier]
            if names:
                hint = ("该型号在供应商「%s」的历史账单中存在，当前所选供应商「%s」下没有记录，"
                        "请确认供应商是否选错" % ("、".join(names), supplier))
                sugg = names
        except Exception:
            pass
        msg = ("历史账单中没有与「%s」相同或相近的型号（供应商「%s」）："
               "可手工填写尺寸/材质后继续下单，或核对型号写法" % (model_raw, supplier))
        if hint:
            msg = hint
        return dict(blank, message=msg, supplier_hint=hint, suggestions=sugg)
    finally:
        conn.close()


def get_order(order_no):
    conn = db.connect()
    row = conn.execute("SELECT * FROM po_order WHERE order_no=?", (order_no,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_orders(role, supplier=None, status=None):
    """订单列表：供应商角色只能看自己家的订单。"""
    where, args = [], []
    if role == ROLE_SUPPLIER:
        where.append("supplier=?")
        args.append(supplier or "")
    if status:
        where.append("status=?")
        args.append(status)
    sql = "SELECT * FROM po_order"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT 200"
    conn = db.connect()
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_events(order_no):
    conn = db.connect()
    rows = conn.execute("SELECT * FROM po_event WHERE order_no=? ORDER BY id", (order_no,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _transition(order_no, from_status, to_status, sets, event_action, actor, role, detail):
    """带前置状态条件的原子流转：并发/重复操作下不会重复推进。"""
    extra_keys = list(sets.keys())
    sql = "UPDATE po_order SET status=?" + "".join(", %s=?" % k for k in extra_keys) + \
          " WHERE order_no=? AND status=?"
    args = [to_status] + [sets[k] for k in extra_keys] + [order_no, from_status]
    conn = db.connect()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status, supplier FROM po_order WHERE order_no=?", (order_no,)).fetchone()
        if not row:
            raise ValueError("订单不存在：%s" % order_no)
        if row["status"] != from_status:
            raise ValueError("订单当前状态为「%s」，不能执行该操作（可能已被处理，请刷新）"
                             % STATUS_LABELS.get(row["status"], row["status"]))
        cur = conn.execute(sql, args)
        if cur.rowcount == 0:
            raise ValueError("订单状态已变更，操作未生效，请刷新后重试")
        conn.execute(
            "INSERT INTO po_event(order_no,action,actor,role,detail,created_at) VALUES(?,?,?,?,?,?)",
            (order_no, event_action, actor, role, detail, now()),
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return get_order(order_no)


def confirm_order(order_no, actor):
    """供应商确认接单：pending_confirm → confirmed。"""
    return _transition(order_no, STATUS_PENDING, STATUS_CONFIRMED,
                       {"confirmed_at": now(), "confirmed_by": actor},
                       "confirm", actor, ROLE_SUPPLIER, "供应商确认接单")


def reject_order(order_no, reason, actor):
    """供应商拒接：pending_confirm → rejected（流程终止）。"""
    reason = (reason or "").strip() or "未填写原因"
    return _transition(order_no, STATUS_PENDING, STATUS_REJECTED,
                       {"reject_reason": reason, "confirmed_at": now(), "confirmed_by": actor},
                       "reject", actor, ROLE_SUPPLIER, "供应商拒接：%s" % reason)


def receive_order(order_no, received_qty, received_date, remark, receipt_name, receipt_path, actor):
    """采购方登记收货：confirmed → received。"""
    return _transition(order_no, STATUS_CONFIRMED, STATUS_RECEIVED,
                       {"received_at": now(), "received_qty": received_qty,
                        "received_date": received_date, "receive_remark": remark,
                        "receipt_name": receipt_name, "receipt_path": receipt_path},
                       "receive", actor, ROLE_PURCHASER,
                       "登记到货：%g，到货日期 %s%s" % (received_qty, received_date,
                                                 ("，凭证：" + receipt_name) if receipt_name else ""))


def stats():
    conn = db.connect()
    rows = conn.execute("SELECT status, COUNT(*) n FROM po_order GROUP BY status").fetchall()
    conn.close()
    out = {s: 0 for s in STATUS_LABELS}
    for r in rows:
        out[r["status"]] = r["n"]
    out["total"] = sum(out.values())
    return out
