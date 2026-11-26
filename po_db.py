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
        """SELECT model, model_norm, size, material, unit_price, period
           FROM hist_price WHERE supplier=? ORDER BY period DESC, id DESC""",
        (supplier,),
    ).fetchall()


def _fuzzy_candidates(rows, norm, model_raw):
    """近似候选：先按忽略分隔符的宽松键，再按前缀/包含关系。返回 (命中行, 匹配说明)。"""
    key = parsers.model_key(model_raw)
    if key:
        same = [r for r in rows if parsers.model_key(r["model"]) == key]
        if same:
            return same, "忽略分隔符后一致"
    pre = [r for r in rows
           if r["model_norm"] and norm and
           (r["model_norm"].startswith(norm) or norm.startswith(r["model_norm"]))]
    if pre:
        return pre, "前缀/包含关系"
    return [], ""


def lookup_model(supplier, model_raw):
    """按「供应商 + 型号（归一化）」从历史账单匹配，返回回填数据与匹配状态。

    分层策略（修复缺陷 P1-5：型号写法差异导致「历史有却显示未找到」）：
      ① 精确：supplier + model_norm 全等（走索引，热路径）；
      ② 近似：同一供应商下忽略分隔符差异 / 前缀包含关系 → 标记 match_type=fuzzy，
         只回填尺寸与材料、不回填单价，前端强制提示「请核对型号」；
      ③ 未命中时做跨供应商诊断：型号在其它供应商历史里存在则明确提示换供应商。
    返回 dict:
      found / match_type(exact|fuzzy|none) / matches / risk
      size / material / param / unit_price
      matched_model: 命中的历史型号原文
      suggestions: 近似型号列表（最多 5 个，供人工核对）
      supplier_hint: 型号存在于其它供应商时的提示
      message: 面向采购人员的提示
    """
    norm = parsers.normalize_model(model_raw or "")
    blank = {"found": False, "match_type": "none", "matches": 0, "risk": False,
             "size": "", "material": "", "param": "", "unit_price": None,
             "matched_model": "", "suggestions": [], "supplier_hint": ""}
    if not norm:
        return dict(blank, message="请输入型号")
    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT model, size, material, unit_price, period
               FROM hist_price WHERE supplier=? AND model_norm=?
               ORDER BY period DESC, id DESC""",
            (supplier, norm),
        ).fetchall()
        match_type = "exact"
        if not rows:
            # ② 近似匹配（仅同供应商，避免串料）
            cands, how = _fuzzy_candidates(_load_supplier_rows(conn, supplier), norm, model_raw)
            if cands:
                rows, match_type = cands, "fuzzy"
                sugg = []
                for r in cands:
                    m = str(r["model"] or "").strip()
                    if m and m not in sugg:
                        sugg.append(m)
                return _fmt_result(rows, match_type, supplier, model_raw,
                                   matched_model=sugg[0], suggestions=sugg[:5],
                                   fuzzy_how=how)
        if rows:
            return _fmt_result(rows, "exact", supplier, model_raw,
                               matched_model=str(rows[0]["model"] or "").strip())

        # ③ 跨供应商诊断
        hint, sugg = "", []
        try:
            others = conn.execute(
                "SELECT DISTINCT supplier FROM hist_price WHERE model_norm=?", (norm,)
            ).fetchall()
            names = [str(r["supplier"]) for r in others if r["supplier"] and r["supplier"] != supplier]
            if names:
                hint = ("该型号在供应商「%s」的历史中存在，当前所选供应商「%s」下没有记录，"
                        "请确认供应商是否选错" % ("、".join(names), supplier))
                sugg = names
        except Exception:
            pass
        msg = ("未找到该型号的历史记录（供应商「%s」+ 型号「%s」）："
               "可手工填写参数/尺寸/材料后继续下单，或核对型号是否与历史账单一致" % (supplier, model_raw))
        if hint:
            msg = hint
        return dict(blank, message=msg, supplier_hint=hint, suggestions=sugg)
    finally:
        conn.close()


def _fmt_result(rows, match_type, supplier, model_raw, matched_model="",
                suggestions=None, fuzzy_how=""):
    """把命中的历史行整理成回填结果。fuzzy 时不回填单价（防止报错价）。"""
    prices = sorted({round(r["unit_price"], 4) for r in rows
                     if r["unit_price"] and r["unit_price"] > 0})
    size = next((r["size"] for r in rows if r["size"]), "")
    material = next((r["material"] for r in rows if r["material"]), "")
    price_txt = " / ".join("¥%g" % p for p in prices) if prices else "无有效价"
    risk = len(prices) >= 2
    if match_type == "fuzzy":
        msg = ("型号「%s」没有精确记录，按近似型号「%s」（%s）匹配到 %d 条历史：已回填尺寸/材料，"
               "单价未回填，请核对型号是否为同一个零件"
               % (model_raw, matched_model or "—", fuzzy_how or "近似", len(rows)))
    elif risk:
        msg = ("匹配到 %d 条历史记录，存在 %d 档单价（%s），已回填最低价 ¥%g，请人工确认适用价"
               % (len(rows), len(prices), price_txt, prices[0]))
    elif prices:
        msg = "已匹配 %d 条历史记录并回填（单价 ¥%g）" % (len(rows), prices[0])
    else:
        msg = "匹配到 %d 条历史记录，但无有效单价" % len(rows)
    return {
        "found": True, "match_type": match_type, "matches": len(rows), "risk": risk,
        "size": size, "material": material,
        "param": "%d 档价 %s" % (len(prices), price_txt),
        "unit_price": (prices[0] if prices and match_type == "exact" else None),
        "matched_model": matched_model,
        "suggestions": suggestions or [],
        "supplier_hint": "",
        "message": msg,
    }


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
