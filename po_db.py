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
"""

# 种子账号（首次启动创建；密码请首次登录后按需修改 config）
SEED_USERS = [
    ("purchaser", "123456", ROLE_PURCHASER, None, "采购员"),
    ("jiesheng", "123456", ROLE_SUPPLIER, "杰盛", "杰盛（供应商）"),
    ("xinxin", "123456", ROLE_SUPPLIER, "新新精密", "新新精密（供应商）"),
    ("yunjin", "123456", ROLE_SUPPLIER, "云锦", "云锦（供应商）"),
]

# 图纸/凭证允许的扩展名
DOC_EXTS = (".pdf", ".dwg", ".dxf", ".png", ".jpg", ".jpeg", ".xlsx", ".xls")


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

def create_order(data, drawing_name, drawing_path, actor, order_no=None):
    """创建订单并登记 create 事件。返回订单 dict。

    order_no 可由调用方预先生成（便于先按单号落图纸文件），缺省自动生成。
    """
    order_no = order_no or new_order_no()
    qty = float(data["quantity"])
    price = data.get("unit_price")
    total = round(qty * price, 2) if price is not None else None
    conn = db.connect()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO po_order
            (order_no,supplier,purchaser,product_name,model,quantity,unit,unit_price,total_amount,
             required_date,remark,drawing_name,drawing_path,status,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (order_no, data["supplier"], actor, data["product_name"], data.get("model", ""),
             qty, data.get("unit", ""), price, total, data.get("required_date", ""),
             data.get("remark", ""), drawing_name, drawing_path, STATUS_PENDING, now()),
        )
        conn.execute(
            "INSERT INTO po_event(order_no,action,actor,role,detail,created_at) VALUES(?,?,?,?,?,?)",
            (order_no, "create", actor, ROLE_PURCHASER,
             "提交下单：%s × %g%s，供应商 %s" % (data["product_name"], qty,
                                          data.get("unit", "") or "", data["supplier"]), now()),
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
