# -*- coding: utf-8 -*-
"""SQLite 数据层：连接、建表、CRUD、事务化导入、备份。

本次重构（上线前复盘 A-3/B-1/B-3）：
1. 历史导入改为单事务聚合 import_history()：批次+明细+价格索引在同一事务内提交，
   任一环节失败整体回滚，杜绝"部分提交 + 索引不同步"（缺陷 P1-3）；
2. hist_price 补 quantity/total_amount/bill_date/remark 四列，数量与合计金额不再
   被静默丢弃（缺陷 P2-7），ALTER 兼容旧库；
3. clear_all() 清空后执行 VACUUM 回收空间（缺陷 P3-9）；
4. backup_db() 用 SQLite backup API 做一致性快照备份；
5. load_price_index() 全量预载，matcher 不再逐行建连接（缺陷 P3-18）。
"""
import os
import re
import hashlib
import sqlite3
import json
import unicodedata
from datetime import datetime

from config import DB_PATH, BACKUP_DIR, BACKUP_KEEP, SEED_SUPPLIERS

SCHEMA = """
CREATE TABLE IF NOT EXISTS import_batch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL,              -- history / pending
    file_name TEXT NOT NULL,
    supplier TEXT,
    total_rows INTEGER DEFAULT 0,
    valid_rows INTEGER DEFAULT 0,
    excluded_rows INTEGER DEFAULT 0,
    error_rows INTEGER DEFAULT 0,
    status TEXT DEFAULT '处理中',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS hist_price (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no TEXT NOT NULL,
    supplier TEXT NOT NULL,
    model TEXT NOT NULL,
    model_norm TEXT NOT NULL,
    material TEXT,
    size TEXT,
    quantity REAL,                   -- 数量（记录用，不参与匹配）
    total_amount REAL,               -- 合计金额（记录用，不参与匹配）
    bill_date TEXT,                  -- 账单日期 YYYY-MM-DD（记录用）
    remark TEXT,                     -- 备注（记录用）
    unit_price REAL,
    period TEXT,
    source_file TEXT,
    source_row INTEGER,
    exclude_reason TEXT,             -- 空则有效价；非空则被排除
    dedup_hash TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_hist_model ON hist_price(supplier, model_norm);
CREATE INDEX IF NOT EXISTS idx_hist_norm ON hist_price(model_norm);
CREATE UNIQUE INDEX IF NOT EXISTS idx_hist_dedup ON hist_price(dedup_hash);

CREATE TABLE IF NOT EXISTS price_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier TEXT NOT NULL,
    model TEXT NOT NULL,
    model_norm TEXT NOT NULL,
    materials TEXT,
    min_price REAL,
    latest_price REAL,
    price_levels INTEGER DEFAULT 1,
    price_detail TEXT,               -- JSON: [{period, price}]
    risk_flag INTEGER DEFAULT 0,     -- 1=同型号多档价，需复核
    updated_at TEXT,
    UNIQUE(supplier, model_norm)
);

CREATE TABLE IF NOT EXISTS recon_batch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recon_no TEXT UNIQUE NOT NULL,
    file_name TEXT,
    headers_json TEXT,               -- 上传账单原始表头（JSON 数组，含 * 与列顺序）
    mapping_diff_json TEXT,          -- 字段映射差异说明（JSON 数组）
    total_rows INTEGER DEFAULT 0,
    n_matched INTEGER DEFAULT 0,
    n_review INTEGER DEFAULT 0,
    n_above INTEGER DEFAULT 0,
    n_nohistory INTEGER DEFAULT 0,
    n_error INTEGER DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS recon_result (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recon_no TEXT NOT NULL,
    supplier TEXT,
    model TEXT,
    model_norm TEXT,
    material TEXT,
    size TEXT,
    bill_price REAL,
    period TEXT,
    source_file TEXT,
    source_row INTEGER,
    status TEXT NOT NULL,
    lowest_price REAL,
    evidence TEXT,                   -- 依据说明
    reason TEXT,                     -- 复核原因
    raw_values_json TEXT,            -- 上传账单该行原始单元格值（JSON 数组，按表头顺序）
    -- 智能对账：差异字段（本单 vs 历史基准）
    quantity REAL,                   -- 数量（用于金额差异换算）
    bill_amount REAL,                -- 本单合计金额（上传账单「合计金额」列）
    bill_date TEXT,                  -- 本单账单日期 YYYY-MM-DD
    price_gap REAL,                  -- 单价差异 = 本单单价 − 历史最低价
    amount_gap REAL,                 -- 金额差异 = 单价差异 × 数量
    gap_rate REAL,                   -- 差异率 = 单价差异 / 历史最低价
    diff_types TEXT,                 -- 差异类型（逗号分隔的代码，见 config.DIFF_LABELS）
    hist_period TEXT,                -- 历史最低价所在期间 YYYY-MM
    date_gap INTEGER,                -- 账单月 − 基准月（月数，正数=基准更早）
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_result_batch ON recon_result(recon_no);
CREATE INDEX IF NOT EXISTS idx_result_status ON recon_result(status);

-- 供应商主数据：供应商不再写死在代码里，由此表驱动（可在页面「供应商管理」中新增/停用）
CREATE TABLE IF NOT EXISTS supplier (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,       -- 规范名称（与账单「供应商」列一致）
    norm TEXT UNIQUE NOT NULL,       -- 归一化名称（查重用：去空格、全角转半角、大写）
    alias TEXT,                      -- 别名（逗号分隔，用于按文件名/表头自动识别）
    remark TEXT,
    active INTEGER DEFAULT 1,        -- 1=可选；0=停用（历史数据仍保留）
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_supplier_active ON supplier(active, name);

-- 供应商历史有效数据台账：按「供应商 × 期间」聚合的快照，写入后自动重算（唯一约束保证不重复写）
CREATE TABLE IF NOT EXISTS supplier_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier TEXT NOT NULL,
    period TEXT NOT NULL,            -- YYYY-MM（无月份记为「未标月份」）
    rows_total INTEGER DEFAULT 0,    -- 该期明细条数
    rows_valid INTEGER DEFAULT 0,    -- 有效明细条数（有效价：非空价/非待核价/单价>0）
    models INTEGER DEFAULT 0,        -- 型号数
    min_price REAL,                  -- 当期最低有效单价
    avg_price REAL,                  -- 当期平均有效单价
    amount_total REAL,               -- 金额合计（合计金额列，缺失时用 数量×单价）
    first_date TEXT, last_date TEXT, -- 数据时间范围
    files TEXT, batches TEXT,        -- 来源：文件 / 批次号
    status TEXT,                     -- new 新增 / changed 变更 / steady 无变化 / expired 失效
    change_note TEXT,                -- 与上期相比的变化说明
    delta_rows INTEGER, delta_models INTEGER, delta_amount REAL,
    created_at TEXT, updated_at TEXT,
    UNIQUE(supplier, period)
);
CREATE INDEX IF NOT EXISTS idx_sup_hist ON supplier_history(supplier, period);

-- 供应商数据状态变化事件（新增/变更/失效），dedup 唯一，保证同一变化只记一次
CREATE TABLE IF NOT EXISTS supplier_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier TEXT, period TEXT,
    event TEXT,                      -- new / changed / expired / restored
    detail TEXT,                     -- 变化说明（指标前后对比）
    ref TEXT,                        -- 触发来源（批次号 / 初始化 / 清空）
    at TEXT,
    dedup TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_sup_event ON supplier_event(supplier, at);
"""


def _jdumps(obj):
    """安全 JSON 序列化：日期等对象转字符串。"""
    def conv(o):
        if hasattr(o, "isoformat"):
            return o.isoformat()
        return str(o)
    return json.dumps(obj, ensure_ascii=False, default=conv)


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    # --- 只影响性能、不改变持久化语义的几项 ---
    # 每次请求都新建连接，默认 2MB 页缓存偏小；NAS 上一个核对批次会反复扫 price_index。
    conn.execute("PRAGMA cache_size=-16000")        # 16MB 页缓存
    conn.execute("PRAGMA temp_store=MEMORY")        # 排序/临时表走内存，不落盘
    conn.execute("PRAGMA mmap_size=134217728")      # 128MB 内存映射读，减少 read() 系统调用
    # synchronous 默认 FULL（最安全）。WAL 下改 NORMAL 可显著减少 fsync，
    # 代价是断电/强杀时可能丢最后几笔已提交事务，因此做成开关、默认不开。
    if os.environ.get("RECON_SQLITE_FAST", "0").strip().lower() in ("1", "true", "yes"):
        conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    # 兼容已有数据库：缺列时补建
    def add_cols(table, cols):
        existing = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
        for name, typ in cols.items():
            if name not in existing:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, typ))
    add_cols("recon_batch", {"headers_json": "TEXT", "mapping_diff_json": "TEXT"})
    add_cols("recon_result", {"raw_values_json": "TEXT"})
    # 智能对账：差异字段（老库升级时补列，历史批次这些字段为空，前端按「—」展示）
    add_cols("recon_result", {
        "quantity": "REAL", "bill_amount": "REAL", "bill_date": "TEXT",
        "price_gap": "REAL", "amount_gap": "REAL", "gap_rate": "REAL",
        "diff_types": "TEXT", "hist_period": "TEXT", "date_gap": "INTEGER",
    })
    add_cols("hist_price", {"quantity": "REAL", "total_amount": "REAL",
                            "bill_date": "TEXT", "remark": "TEXT"})
    conn.commit()
    conn.close()
    seed_suppliers()


# ---------- 供应商主数据 ----------

def supplier_norm(name):
    """供应商名归一化（查重键）：全角转半角、去空白、大写。

    用于识别「云锦 」「ＹＵＮＪＩＮ」「云锦」这类同义写法，避免重复建档。
    """
    s = unicodedata.normalize("NFKC", str(name or ""))
    return re.sub(r"\s+", "", s).upper()


def seed_suppliers():
    """首次建库/升级时补齐供应商主数据。

    - 代码里只保留「初始种子」（config.SEED_SUPPLIERS），之后一律以数据库为准；
    - 同时把历史数据里已出现过、但主数据里没有的供应商自动补建，
      保证升级后旧账单的供应商不会因为「不在列表里」而无法再用。
    返回新增条数。
    """
    conn = connect()
    existing = {r["norm"] for r in conn.execute("SELECT norm FROM supplier")}
    added = 0

    def _add(name, alias="", remark=""):
        nonlocal added
        n = str(name or "").strip()
        if not n:
            return
        nm = supplier_norm(n)
        if not nm or nm in existing:
            return
        conn.execute(
            "INSERT OR IGNORE INTO supplier(name,norm,alias,remark,active,created_at) VALUES(?,?,?,?,1,?)",
            (n, nm, alias or "", remark or "初始内置", now()),
        )
        existing.add(nm)
        added += 1

    for item in SEED_SUPPLIERS:
        name, alias = (item if isinstance(item, (tuple, list)) else (item, ""))
        _add(name, alias, "初始内置")
    # 历史数据里出现过的供应商，自动补进主数据（升级兼容）
    for r in conn.execute(
        "SELECT DISTINCT supplier FROM hist_price WHERE supplier IS NOT NULL AND supplier<>''"
    ).fetchall():
        _add(r["supplier"], "", "由历史账单自动补建")
    conn.commit()
    conn.close()
    return added


def list_suppliers(active_only=False, with_usage=False):
    """供应商列表（按建档顺序，新增加的追加在末尾）。with_usage=True 时附带历史/核对引用数。"""
    sql = "SELECT * FROM supplier"
    if active_only:
        sql += " WHERE active=1"
    sql += " ORDER BY id"
    conn = connect()
    rows = [dict(r) for r in conn.execute(sql)]
    if with_usage:
        hist = {r[0]: r[1] for r in conn.execute(
            "SELECT supplier, COUNT(*) FROM hist_price GROUP BY supplier")}
        recon = {r[0]: r[1] for r in conn.execute(
            "SELECT supplier, COUNT(*) FROM recon_result GROUP BY supplier")}
        for r in rows:
            r["hist_rows"] = hist.get(r["name"], 0)
            r["recon_rows"] = recon.get(r["name"], 0)
            r["in_use"] = bool(r["hist_rows"] or r["recon_rows"])
    conn.close()
    return rows


def supplier_names(active_only=True):
    return [r["name"] for r in list_suppliers(active_only=active_only)]


def supplier_exists(name):
    return supplier_norm(name) in {r["norm"] for r in list_suppliers()}


def add_supplier(name, alias="", remark=""):
    """新增供应商。校验：必填 / 长度 / 非法字符 / 重名（忽略空格与全半角差异）。

    返回新建记录 dict；校验失败抛 ValueError（带面向用户的中文原因）。
    """
    raw = str(name or "").strip()
    if not raw:
        raise ValueError("「供应商名称」不能为空")
    display = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", raw)).strip()
    if len(display) > 40:
        raise ValueError("「供应商名称」最长 40 个字符（当前 %d）" % len(display))
    if re.search(r"[\\/:*?\"<>|\n\r\t]", display):
        raise ValueError("「供应商名称」不能包含 \\ / : * ? \" < > | 等特殊字符")
    nm = supplier_norm(display)
    if len(nm) < 1:
        raise ValueError("「供应商名称」无效，请填写有效名称")

    conn = connect()
    try:
        dup = conn.execute("SELECT name FROM supplier WHERE norm=?", (nm,)).fetchone()
        if dup:
            raise ValueError("供应商「%s」已存在（忽略空格/全半角差异视为同一家），请勿重复添加"
                             % dup["name"])
        conn.execute(
            "INSERT INTO supplier(name,norm,alias,remark,active,created_at) VALUES(?,?,?,?,1,?)",
            (display, nm, str(alias or "").strip(), str(remark or "").strip(), now()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM supplier WHERE norm=?", (nm,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def delete_supplier(sid):
    """删除供应商：仅允许删除没有任何历史/核对引用的供应商（有引用则要求先停用）。"""
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM supplier WHERE id=?", (sid,)).fetchone()
        if not row:
            raise ValueError("供应商不存在（可能已被删除）")
        name = row["name"]
        h = conn.execute("SELECT COUNT(*) FROM hist_price WHERE supplier=?", (name,)).fetchone()[0]
        r = conn.execute("SELECT COUNT(*) FROM recon_result WHERE supplier=?", (name,)).fetchone()[0]
        if h or r:
            raise ValueError("供应商「%s」已被使用（历史 %d 条、核对 %d 条），不能删除；"
                             "如只是不再合作，可考虑保留该档案" % (name, h, r))
        conn.execute("DELETE FROM supplier WHERE id=?", (sid,))
        conn.commit()
        return name
    finally:
        conn.close()


def supplier_alias_map():
    """别名 → 规范名 的映射（用于按文件名/表头自动识别供应商）。

    来源：supplier 表的 name 与 alias（alias 支持 , 、 ; 空格 分隔）。
    """
    out = {}
    for r in list_suppliers():
        name = r["name"]
        out[name] = name
        for a in re.split(r"[,，、;；/\s]+", r.get("alias") or ""):
            a = a.strip()
            if a:
                out[a] = name
    return out


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- 供应商历史有效数据（按供应商 × 期间聚合的台账） ----------

# 有效明细口径：不含空价/待核价等排除行，且单价必须 > 0
VALID_COND = "COALESCE(exclude_reason,'')='' AND unit_price IS NOT NULL AND unit_price>0"

SUP_HIST_LABELS = {"new": "新增", "changed": "变更", "steady": "无变化",
                   "expired": "失效", "restored": "恢复"}


def _hist_agg(conn):
    """按「供应商 × 期间」聚合 hist_price 的关键指标。"""
    v = VALID_COND
    sql = """
        SELECT supplier,
               COALESCE(NULLIF(period,''), '未标月份') AS period,
               COUNT(*) AS rows_total,
               SUM(CASE WHEN {v} THEN 1 ELSE 0 END) AS rows_valid,
               COUNT(DISTINCT CASE WHEN {v} THEN model_norm END) AS models,
               MIN(CASE WHEN {v} THEN unit_price END) AS min_price,
               AVG(CASE WHEN {v} THEN unit_price END) AS avg_price,
               SUM(CASE WHEN total_amount IS NOT NULL AND total_amount<>0 THEN total_amount
                        ELSE quantity*unit_price END) AS amount_total,
               MIN(bill_date) AS first_date, MAX(bill_date) AS last_date,
               GROUP_CONCAT(DISTINCT source_file) AS files,
               GROUP_CONCAT(DISTINCT batch_no) AS batches
        FROM hist_price
        GROUP BY supplier, period
    """.format(v=v)
    return {(r["supplier"], r["period"]): dict(r) for r in conn.execute(sql)}


def _log_supplier_event(conn, supplier, period, event, detail, ref):
    """记录一条状态变化事件；同一变化（含指标）只记一次。返回 1=新写入。"""
    dedup = hashlib.md5(("%s|%s|%s|%s" % (supplier, period, event, detail)).encode("utf-8")).hexdigest()
    cur = conn.execute(
        "INSERT OR IGNORE INTO supplier_event(supplier,period,event,detail,ref,at,dedup) VALUES(?,?,?,?,?,?,?)",
        (supplier, period, event, detail, ref, now(), dedup))
    return 1 if cur.rowcount else 0


def rebuild_supplier_history(ref="", conn=None):
    """重算供应商历史有效数据快照，并记录「新增/变更/失效/恢复」事件。

    - 幂等：唯一键 (supplier, period) + upsert；指标无变化时不写库、不产生事件；
    - 无效或过期数据不计入：只有有效价明细参与 rows_valid / models / min_price / avg_price，
      整期无有效数据时 status=expired，且不会计入页面的「有效记录」合计。
    返回 {"changed": 更新条数, "events": 新事件数}。
    """
    own = conn is None
    conn = conn or connect()
    try:
        agg = _hist_agg(conn)
        prev = {(r["supplier"], r["period"]): dict(r)
                for r in conn.execute("SELECT * FROM supplier_history")}
        keys = set(agg) | set(prev)
        changed = events = 0
        for key in sorted(keys):
            supplier, period = key
            cur = agg.get(key) or {
                "supplier": supplier, "period": period, "rows_total": 0, "rows_valid": 0,
                "models": 0, "min_price": None, "avg_price": None, "amount_total": 0.0,
                "first_date": None, "last_date": None, "files": None, "batches": None,
            }
            old = prev.get(key)
            d_rows = d_models = 0
            d_amount = 0.0
            note = ""

            if not cur["rows_total"]:
                # 整期已无数据 → 失效（保留一行用于展示状态变化，但不计入有效合计）
                if old is None:
                    continue
                if old.get("status") == "expired" and not (old.get("rows_valid") or 0):
                    # 早已是失效态：既不再写行也不记事件。
                    # 否则每次重启/重建都会因文案里「原有效明细 N 条」由上一条的 rows_valid
                    # 变成 0 而被判成「变更」，反复刷新 updated_at 并堆出重复的失效事件。
                    continue
                status = "expired"
                note = "该期间已无数据（被删除或清空），原有效明细 %d 条" % (old["rows_valid"] or 0)
                events += _log_supplier_event(conn, supplier, period, "expired", note, ref)
            elif old is None:
                status = "new"
                note = "首次登记：明细 %d 条、有效 %d 条、型号 %d 个" % (
                    cur["rows_total"], cur["rows_valid"], cur["models"])
                events += _log_supplier_event(conn, supplier, period, "new", note, ref)
            else:
                d_rows = cur["rows_total"] - (old["rows_total"] or 0)
                d_models = (cur["models"] or 0) - (old["models"] or 0)
                d_amount = round((cur["amount_total"] or 0) - (old["amount_total"] or 0), 2)
                same = (cur["rows_total"] == old["rows_total"]
                        and cur["rows_valid"] == old["rows_valid"]
                        and (cur["models"] or 0) == (old["models"] or 0)
                        and abs((cur["amount_total"] or 0) - (old["amount_total"] or 0)) < 0.005
                        and abs((cur["min_price"] or 0) - (old["min_price"] or 0)) < 1e-9)
                if same:
                    continue      # 指标未变：不更新时间、不产生事件（避免无意义的“实时刷新”）
                if not (old["rows_valid"] or 0):
                    status = "restored"
                    note = "期间数据恢复：有效明细 0 → %d 条" % cur["rows_valid"]
                    events += _log_supplier_event(conn, supplier, period, "restored", note, ref)
                else:
                    status = "changed"
                    note = "明细 %+d 条、型号 %+d 个、金额 %+.2f" % (d_rows, d_models, d_amount)
                    events += _log_supplier_event(conn, supplier, period, "changed", note, ref)

            conn.execute(
                """INSERT INTO supplier_history
                   (supplier,period,rows_total,rows_valid,models,min_price,avg_price,amount_total,
                    first_date,last_date,files,batches,status,change_note,delta_rows,delta_models,
                    delta_amount,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(supplier,period) DO UPDATE SET
                     rows_total=excluded.rows_total, rows_valid=excluded.rows_valid,
                     models=excluded.models, min_price=excluded.min_price,
                     avg_price=excluded.avg_price, amount_total=excluded.amount_total,
                     first_date=excluded.first_date, last_date=excluded.last_date,
                     files=excluded.files, batches=excluded.batches, status=excluded.status,
                     change_note=excluded.change_note, delta_rows=excluded.delta_rows,
                     delta_models=excluded.delta_models, delta_amount=excluded.delta_amount,
                     updated_at=excluded.updated_at""",
                (supplier, period, cur["rows_total"], cur["rows_valid"], cur["models"],
                 cur["min_price"], None if cur["avg_price"] is None else round(cur["avg_price"], 4),
                 round(cur["amount_total"] or 0, 2), cur["first_date"], cur["last_date"],
                 cur["files"], cur["batches"], status, note, d_rows, d_models, d_amount,
                 now(), now()))
            changed += 1
        if own:
            conn.commit()
        return {"changed": changed, "events": events}
    finally:
        if own:
            conn.close()


def _like_escape(s):
    """转义 LIKE 通配符：型号里带 % _ \\ 时不至于把关键字当通配符用。"""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _model_hit_map(conn, keyword, supplier=None, month_from="", month_to=""):
    """型号关键字（包含匹配）→ 命中的「供应商 × 期间」及其命中型号。

    匹配口径：
    - 用 normalize_model() 归一化关键字，再对 hist_price.model_norm 做包含匹配，
      所以大小写、全半角、空格、直径符号差异都不影响命中；
    - 只统计**有效价明细**（VALID_COND），与「供应商历史有效数据」的有效口径一致；
    - 返回 (hits, model_total)：hits = {(supplier, period): {"models": [(型号, 条数)], "rows": n}}。
    """
    from parsers import normalize_model   # 延迟导入：入库与查询共用同一归一化函数
    kw = normalize_model(keyword)
    if not kw:
        return {}, 0
    where = ["model_norm LIKE ? ESCAPE '\\'", "(%s)" % VALID_COND]
    args = ["%" + _like_escape(kw) + "%"]
    if supplier:
        where.append("supplier=?")
        args.append(supplier)
    if month_from:
        where.append("COALESCE(NULLIF(period,''),'未标月份')>=?")
        args.append(month_from)
    if month_to:
        where.append("COALESCE(NULLIF(period,''),'未标月份')<=?")
        args.append(month_to)
    rows = conn.execute(
        """SELECT supplier,
                  COALESCE(NULLIF(period,''),'未标月份') AS period,
                  model_norm, MIN(model) AS model, COUNT(*) AS hits
           FROM hist_price WHERE %s
           GROUP BY supplier, period, model_norm
           ORDER BY supplier, period, model_norm""" % " AND ".join(where), args).fetchall()
    hits = {}
    for r in rows:
        key = (r["supplier"], r["period"])
        h = hits.setdefault(key, {"models": [], "rows": 0})
        h["models"].append((r["model"], r["hits"]))
        h["rows"] += r["hits"]
    return hits, len({r["model_norm"] for r in rows})


def list_supplier_history(supplier=None, month_from="", month_to="", include_expired=True, model=""):
    """按供应商 + 月份范围 + 型号关键字查询历史有效数据（含状态、来源、指标）。

    model 非空时：只保留「含该型号（包含匹配）」的期间，并在每行附带
    models_hit（命中型号数）/ rows_hit（命中明细条数）/ model_names（命中型号名，最多 6 个），
    第二个返回值改为「命中型号总数」。整个查询只读，不写库、不产生事件。
    """
    sql = "SELECT * FROM supplier_history WHERE 1=1"
    args = []
    if supplier:
        sql += " AND supplier=?"
        args.append(supplier)
    if month_from:
        sql += " AND period>=?"       # 「未标月份」在范围查询时天然被排除（不算有效记录）
        args.append(month_from)
    if month_to:
        sql += " AND period<=?"
        args.append(month_to)
    if not include_expired:
        sql += " AND rows_valid>0"
    # 有效期在前（按期间升序），失效期排到最后（保留痕迹但不干扰阅读）
    sql += " ORDER BY supplier, CASE WHEN rows_valid>0 THEN 0 ELSE 1 END, period"
    conn = connect()
    try:
        rows = [dict(r) for r in conn.execute(sql, args)]
        if model:
            # 型号关键字筛选：按命中期间裁剪台账行，并把命中详情挂到行上供前端展示
            hits, hit_models = _model_hit_map(conn, model, supplier, month_from, month_to)
            kept = []
            for r in rows:
                h = hits.get((r["supplier"], r["period"]))
                if not h:
                    continue
                r["models_hit"] = len(h["models"])
                r["rows_hit"] = h["rows"]
                r["model_names"] = "、".join(m for m, _n in h["models"][:6])
                kept.append(r)
            rows = kept
            return rows, hit_models       # 型号数按命中口径返回（跨期去重）
        # 期间数量/型号总数按实际数据口径补全（型号跨期去重）
        where, wargs = [], []
        if supplier:
            where.append("supplier=?"); wargs.append(supplier)
        if month_from:
            where.append("COALESCE(NULLIF(period,''),'未标月份')>=?"); wargs.append(month_from)
        if month_to:
            where.append("COALESCE(NULLIF(period,''),'未标月份')<=?"); wargs.append(month_to)
        wsql = (" WHERE " + " AND ".join(where)) if where else ""
        models = conn.execute(
            "SELECT COUNT(DISTINCT model_norm) FROM hist_price" + wsql +
            ((" AND " if where else " WHERE ") + VALID_COND), wargs).fetchone()[0]
        return rows, models
    finally:
        conn.close()


def list_supplier_events(supplier=None, limit=80):
    sql = "SELECT * FROM supplier_event"
    args = []
    if supplier:
        sql += " WHERE supplier=?"
        args.append(supplier)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def new_no(prefix):
    # 时间戳 + 进程号 + 4位随机，避免同一秒上传多个文件时批次号冲突
    import random
    return prefix + datetime.now().strftime("%Y%m%d%H%M%S") + str(os.getpid() % 100).zfill(2) + str(random.randint(0, 9999)).zfill(4)


# ---------- 历史导入（单事务聚合） ----------

# 去重口径版本：v1 只看「供应商+型号+单价+月份」（会合并同月同价的不同发货明细）；
# v2 起时间与明细内容全部参与，见 dedup_key()。
DEDUP_VERSION = 2


def _norm_text(v):
    return " ".join(str(v if v is not None else "").split())


def dedup_key(r):
    """发货明细去重键（v2）：**时间是区分记录的首要条件**。

    规则：日期/数量/合计金额/备注任一项不同，即为不同发货明细，分别入库、不合并。
    只有「供应商 + 型号 + 单价 + 日期 + 数量 + 合计金额 + 备注」全部相同才算同一条记录
    （用于重复导入/备份回灌时不产生重复数据）。
    """
    return "|".join([
        _norm_text(r.get("supplier")),
        _norm_text(r.get("model_norm") or r.get("model")),
        str(r.get("unit_price")),
        str(r.get("date") or r.get("bill_date") or ""),   # 精确到日，不做月份层面的合并
        str(r.get("period") or ""),
        str(r.get("quantity")),
        str(r.get("total_amount")),
        _norm_text(r.get("remark")),
    ])


def dedup_hash(key, seq):
    """内容键 + 同内容序号 → 哈希。序号保证「同一文件里两条完全相同的明细」各自入库且可幂等去重。"""
    return hashlib.md5(("%s#%d" % (key, seq)).encode("utf-8")).hexdigest()


def migrate_dedup_v2():
    """把既有 hist_price 的 dedup_hash 升级到 v2 口径（幂等，用 PRAGMA user_version 记版本）。

    必须在 refresh_model_norms() 之后执行：v2 键包含 model_norm。
    仅重算哈希、不改动任何业务字段；旧口径下被合并掉的明细已无法从库内恢复，
    需要用原始账单重新导入一次补齐（re-import 只会插入缺失的那些行）。
    返回更新的行数。
    """
    conn = connect()
    try:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if ver >= DEDUP_VERSION:
            return 0
        rows = conn.execute(
            "SELECT id,supplier,model,model_norm,unit_price,period,bill_date,quantity,"
            "total_amount,remark,dedup_hash FROM hist_price ORDER BY id"
        ).fetchall()
        counters, updates = {}, []
        for row in rows:
            d = dict(row)
            key = dedup_key(d)
            counters[key] = counters.get(key, 0) + 1
            h = dedup_hash(key, counters[key])
            if h != d["dedup_hash"]:
                updates.append((h, d["id"]))
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            for h, i in updates:
                conn.execute("UPDATE hist_price SET dedup_hash=? WHERE id=?", (h, i))
            conn.execute("PRAGMA user_version=%d" % DEDUP_VERSION)
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return len(updates)
    finally:
        conn.close()


def _hist_insert_params(r, batch_no, dedup_hash):
    """构造 hist_price 插入参数：显式列出全部列，缺键补 None（兼容旧调用方）。"""
    return {
        "batch_no": batch_no,
        "supplier": r.get("supplier"),
        "model": r.get("model"),
        "model_norm": r.get("model_norm"),
        "material": r.get("material"),
        "size": r.get("size"),
        "quantity": r.get("quantity"),
        "total_amount": r.get("total_amount"),
        "bill_date": r.get("bill_date") or r.get("date"),
        "remark": r.get("remark"),
        "unit_price": r.get("unit_price"),
        "period": r.get("period"),
        "source_file": r.get("source_file"),
        "source_row": r.get("source_row"),
        "exclude_reason": r.get("exclude_reason"),
        "dedup_hash": dedup_hash,
        "created_at": now(),
    }


def _insert_hist_rows_conn(conn, rows):
    """在既有事务/连接内插入历史明细。返回真实插入条数（INSERT OR IGNORE 跳过不计）。"""
    n = 0
    for r in rows:
        before = conn.total_changes
        conn.execute(
            """INSERT OR IGNORE INTO hist_price
            (batch_no,supplier,model,model_norm,material,size,quantity,total_amount,
             bill_date,remark,unit_price,period,source_file,source_row,
             exclude_reason,dedup_hash,created_at)
            VALUES(:batch_no,:supplier,:model,:model_norm,:material,:size,:quantity,
                   :total_amount,:bill_date,:remark,:unit_price,:period,:source_file,
                   :source_row,:exclude_reason,:dedup_hash,:created_at)""",
            r,
        )
        if conn.total_changes > before:
            n += 1
    return n


def _rebuild_price_index_conn(conn):
    """在既有事务/连接内重建型号价格基准（供应商+型号）。返回基准条数。"""
    conn.execute("DELETE FROM price_index")
    rows = conn.execute(
        """SELECT supplier, model, model_norm, material, size, unit_price, period, quantity, exclude_reason
           FROM hist_price ORDER BY supplier, model_norm, id"""
    ).fetchall()

    groups = {}
    for r in rows:
        key = (r["supplier"], r["model_norm"])
        g = groups.setdefault(key, {"supplier": r["supplier"], "model": r["model"],
                                    "model_norm": r["model_norm"], "mats": set(),
                                    "prices": [], "excluded": []})
        if r["material"]:
            g["mats"].add(r["material"])
        if r["exclude_reason"]:
            g["excluded"].append((r["period"], r["exclude_reason"]))
        else:
            # 数量一并进明细：matcher 需要用它判断「低价是否只出现在小批量订单上」
            g["prices"].append((r["period"], r["unit_price"], r["quantity"]))

    import re
    for (supplier, norm), g in groups.items():
        prices = [(p, v, q) for p, v, q in g["prices"] if v is not None and v > 0]
        levels = sorted(set(round(v, 4) for _, v, _ in prices))
        min_price = min(levels) if levels else None
        # 最新价：按 period 月份取最大
        def period_key(pv):
            p = pv[0] or ""
            m = re.search(r"(\d{4})[^\d]*(\d{1,2})", p)
            return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        latest = max(prices, key=period_key)[1] if prices else None
        detail = [{"period": p or "", "price": v, "quantity": q} for p, v, q in prices]
        risk = 1 if len(levels) >= 2 else 0
        conn.execute(
            """INSERT INTO price_index
            (supplier,model,model_norm,materials,min_price,latest_price,price_levels,price_detail,risk_flag,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (supplier, g["model"], norm, "/".join(sorted(g["mats"])), min_price, latest,
             len(levels), _jdumps(detail), risk, now()),
        )
    return len(groups)


def import_history(staged):
    """单事务完成历史导入：批次登记 + 明细写入 + 价格索引重建，全部原子提交。

    staged: [(fname, rows, meta, n_warnings)]
      rows 为 validator 输出的 clean rows（含 quantity/total_amount/date/remark）。
    返回 (reports, added, hist_rows, idx_count)。
    任何异常整体 ROLLBACK，数据库不发生任何变更（修复缺陷 P1-3：
    此前逐文件独立 commit，中途失败会部分提交且索引不重建、报"未提交任何数据"失实）。
    """
    conn = connect()
    conn.isolation_level = None   # 手动事务控制
    reports = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        hist_before = conn.execute("SELECT COUNT(*) FROM hist_price").fetchone()[0]
        for fname, rows, meta, n_warn in staged:
            batch_no = new_no("H")
            conn.execute(
                "INSERT INTO import_batch(batch_no,kind,file_name,supplier,status,created_at) VALUES(?,?,?,?,?,?)",
                (batch_no, "history", fname, meta.get("supplier", ""), "处理中", now()),
            )
            dedup_rows = []
            seen = {}       # 完全相同内容的出现次数（用于区分「同日同价同量的多次发货」）
            for r in rows:
                # 去重键（v2）：时间必须参与 —— 日期不同即不同记录；
                # 数量/合计金额/备注也参与，保证「同日同型号同价但发货量不同」的明细各自入库。
                key = dedup_key(r)
                seen[key] = seen.get(key, 0) + 1
                h = dedup_hash(key, seen[key])
                dedup_rows.append(_hist_insert_params(r, batch_no, h))
            n_inserted = _insert_hist_rows_conn(conn, dedup_rows)
            dup = len(rows) - n_inserted   # 跨批次重复（同内容同时间才算重复）
            conn.execute(
                "UPDATE import_batch SET total_rows=?,valid_rows=?,excluded_rows=?,error_rows=?,status=? WHERE batch_no=?",
                (meta.get("total", 0), meta.get("valid", 0), n_warn, dup, "完成", batch_no),
            )
            reports.append({"file": fname, "ok": True, "supplier": meta.get("supplier", ""),
                            "header_row": meta.get("header_row"), "sheet": meta.get("sheet", ""),
                            "total": meta.get("total", 0), "valid": meta.get("valid", 0),
                            "dup": dup, "batch_no": batch_no})
        idx_count = _rebuild_price_index_conn(conn)
        # 供应商历史有效数据台账：与明细/索引同事务刷新，失败一起回滚
        rebuild_supplier_history(ref=batch_no, conn=conn)
        hist_rows = conn.execute("SELECT COUNT(*) FROM hist_price").fetchone()[0]
        conn.execute("COMMIT")
        added = max(0, hist_rows - hist_before)
        return reports, added, hist_rows, idx_count
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


# ---------- 核对入库（单文件单事务） ----------

def save_recon_file(recon_no, file_name, headers, mapping_diff, results, counts):
    """单事务写入一个核对批次：批次记录 + 全部结果行 + 统计。

    一个核对文件即一个独立批次，事务粒度为文件级：
    失败时该批次完整回滚，不影响已完成的其它批次（如实语义）。
    """
    conn = connect()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO recon_batch(recon_no,file_name,headers_json,mapping_diff_json,created_at) VALUES(?,?,?,?,?)",
            (recon_no, file_name, _jdumps(headers or []), _jdumps(mapping_diff or []), now()),
        )
        for r in results:
            p = dict(r)
            # 差异字段对老调用方可能缺失：统一补 None，保证 INSERT 参数完整
            for k in ("quantity", "bill_amount", "bill_date", "price_gap",
                      "amount_gap", "gap_rate", "diff_types", "hist_period", "date_gap"):
                p.setdefault(k, None)
            conn.execute(
                """INSERT INTO recon_result
                (recon_no,supplier,model,model_norm,material,size,bill_price,period,source_file,
                 source_row,status,lowest_price,evidence,reason,raw_values_json,created_at,
                 quantity,bill_amount,bill_date,price_gap,amount_gap,gap_rate,diff_types,
                 hist_period,date_gap)
                VALUES(:recon_no,:supplier,:model,:model_norm,:material,:size,:bill_price,:period,:source_file,
                       :source_row,:status,:lowest_price,:evidence,:reason,:raw_values_json,:created_at,
                       :quantity,:bill_amount,:bill_date,:price_gap,:amount_gap,:gap_rate,:diff_types,
                       :hist_period,:date_gap)""",
                p,
            )
        conn.execute(
            """UPDATE recon_batch SET total_rows=?,n_matched=?,n_review=?,n_above=?,n_nohistory=?,n_error=?
               WHERE recon_no=?""",
            (counts["total"], counts["matched"], counts["review"], counts["above"],
             counts["nohistory"], counts["error"], recon_no),
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


# ---------- 查询 ----------

def list_history_batches():
    conn = connect()
    rows = conn.execute("SELECT * FROM import_batch WHERE kind='history' ORDER BY id DESC").fetchall()
    conn.close(); return [dict(r) for r in rows]


def get_price_index(supplier, model_norm):
    conn = connect()
    r = conn.execute("SELECT * FROM price_index WHERE supplier=? AND model_norm=?",
                     (supplier, model_norm)).fetchone()
    conn.close(); return dict(r) if r else None


def load_price_index():
    """全量预载价格基准（供应商+型号 -> 基准 dict）。

    修复缺陷 P3-18：matcher 此前每行开新连接查询，600 行账单 = 600 次连接；
    现在核对批次开始时一次性预载，内存 dict 直查。
    """
    conn = connect()
    rows = conn.execute(
        """SELECT supplier, model, model_norm, materials, min_price, latest_price,
                  price_levels, price_detail, risk_flag FROM price_index"""
    ).fetchall()
    conn.close()
    return {(r["supplier"], r["model_norm"]): dict(r) for r in rows}


def hist_count():
    conn = connect()
    n = conn.execute("SELECT COUNT(*) FROM hist_price").fetchone()[0]
    m = conn.execute("SELECT COUNT(*) FROM price_index").fetchone()[0]
    conn.close(); return n, m


def get_recon_batch(recon_no):
    conn = connect()
    r = conn.execute("SELECT * FROM recon_batch WHERE recon_no=?", (recon_no,)).fetchone()
    conn.close(); return dict(r) if r else None


def get_results(recon_no, status=None):
    conn = connect()
    if status:
        rows = conn.execute("SELECT * FROM recon_result WHERE recon_no=? AND status=? ORDER BY id",
                            (recon_no, status)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM recon_result WHERE recon_no=? ORDER BY id",
                            (recon_no,)).fetchall()
    conn.close(); return [dict(r) for r in rows]


def list_recon_batches():
    conn = connect()
    rows = conn.execute("SELECT * FROM recon_batch ORDER BY id DESC LIMIT 50").fetchall()
    conn.close(); return [dict(r) for r in rows]


# ---------- 账单备份（按条件筛选 → 打包） ----------

# 账单月份口径：优先 period（YYYY-MM），缺失时退回 bill_date 的年月
_MONTH_EXPR = "COALESCE(NULLIF(period,''), substr(bill_date,1,7))"


def query_backup_rows(kinds=("history",), suppliers=None, include_excluded=False):
    """按「账单类型 + 供应商」筛选可备份的账单行（不做按月筛选）。

    kinds: history=历史价格库(hist_price) / recon=核对账单(recon_result)
    返回 {"history": [...], "recon": [...]}，元素为统一字段（供应商/日期/型号/尺寸/材质/
    数量/单价/合计金额/备注），可直接写成标准录入模板供后续回灌恢复。
    """
    out = {"history": [], "recon": []}
    suppliers = [s for s in (suppliers or []) if s]
    conn = connect()
    try:
        if "history" in kinds:
            where, args = [], []
            if suppliers:
                where.append("supplier IN (%s)" % ",".join("?" * len(suppliers)))
                args += suppliers
            if not include_excluded:
                where.append("(exclude_reason IS NULL OR exclude_reason='')")
            sql = ("SELECT supplier, model, material, size, quantity, bill_date, period, "
                   "unit_price, total_amount, remark, source_file, source_row, batch_no "
                   "FROM hist_price" + (" WHERE " + " AND ".join(where) if where else "") +
                   " ORDER BY supplier, " + _MONTH_EXPR + ", bill_date, source_row, id")
            out["history"] = [dict(r) for r in conn.execute(sql, args)]
        if "recon" in kinds:
            where, args = [], []
            if suppliers:
                where.append("supplier IN (%s)" % ",".join("?" * len(suppliers)))
                args += suppliers
            sql = ("SELECT supplier, model, material, size, quantity, bill_date, period, "
                   "bill_price AS unit_price, bill_amount AS total_amount, source_file, "
                   "source_row, status, recon_no, diff_types, price_gap, amount_gap "
                   "FROM recon_result" + (" WHERE " + " AND ".join(where) if where else "") +
                   " ORDER BY supplier, " + _MONTH_EXPR + ", bill_date, source_row, id")
            out["recon"] = [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()
    return out


def load_model_key_index():
    """型号宽松键索引：{model_key: [{supplier, model, model_norm, ...基准}]}。

    用于「智能匹配」第二层：精确未命中时，忽略 - _ * 空格等符号差异再试一次，
    命中结果只作参考并标记为需人工复核，不会自动判定为最低价。
    """
    from parsers import model_key
    conn = connect()
    rows = conn.execute(
        """SELECT supplier, model, model_norm, materials, min_price, latest_price,
                  price_levels, price_detail, risk_flag FROM price_index"""
    ).fetchall()
    conn.close()
    idx = {}
    for r in rows:
        d = dict(r)
        idx.setdefault(model_key(d["model"]), []).append(d)
    return idx


def summary_counts(recon_no):
    """批次差异汇总（直接走 SQL 聚合，避免前端再算一遍）。"""
    conn = connect()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(CASE WHEN amount_gap > 0 THEN amount_gap ELSE 0 END), 0) AS overpay,
                      COALESCE(SUM(CASE WHEN amount_gap < 0 THEN amount_gap ELSE 0 END), 0) AS underpay,
                      SUM(CASE WHEN amount_gap IS NOT NULL AND ABS(amount_gap) > 0.0001 THEN 1 ELSE 0 END) AS diff_rows
               FROM recon_result WHERE recon_no=?""", (recon_no,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


# ---------- 型号归一化迁移 ----------

def refresh_model_norms():
    """按当前归一化规则重算 hist_price.model_norm 并重建价格基准。

    修复缺陷 P1-5：归一化规则升级后（全角/乘号/直径符号/尾随 .0/零宽字符），
    旧数据里存的仍是旧口径 model_norm，新查询用新键去匹配会全部落空 —— 表现为
    「型号明明在历史库里，采购端却显示未找到」。启动时执行一次即可，幂等且廉价。

    返回 (改动条数, 价格基准条数)。
    """
    import parsers
    conn = connect()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT id, model, model_norm FROM hist_price").fetchall()
        changed = 0
        for r in rows:
            new = parsers.normalize_model(r["model"])
            if new and new != r["model_norm"]:
                conn.execute("UPDATE hist_price SET model_norm=? WHERE id=?", (new, r["id"]))
                changed += 1
        idx_count = _rebuild_price_index_conn(conn)
        conn.execute("COMMIT")
        return changed, idx_count
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


# ---------- 备份与清空 ----------

def backup_db(tag="manual"):
    """用 SQLite backup API 生成一致性快照备份（WAL 安全）。

    返回备份文件路径；自动只保留最近 BACKUP_KEEP 份（修复缺陷：清空不可恢复且无备份）。
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(BACKUP_DIR, "recon_%s_%s.db" % (tag, ts))
    src = sqlite3.connect(DB_PATH)
    out = sqlite3.connect(dst)
    with out:
        src.backup(out)
    src.close()
    out.close()
    backups = sorted(
        (os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR) if f.endswith(".db")),
        key=os.path.getmtime,
    )
    for old in backups[:-BACKUP_KEEP] if len(backups) > BACKUP_KEEP else []:
        try:
            os.remove(old)
        except OSError:
            pass
    return dst


def clear_all():
    """清空全部业务数据（保留结构）并 VACUUM 回收磁盘空间。

    清空后同步刷新供应商历史台账：各期间转为「失效」，事件留痕（不再计入有效记录）。
    """
    conn = connect()
    for t in ("recon_result", "recon_batch", "hist_price", "price_index", "import_batch"):
        conn.execute("DELETE FROM " + t)
    conn.commit()
    rebuild_supplier_history(ref="清空历史库", conn=conn)
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
