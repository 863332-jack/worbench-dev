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
import hashlib
import sqlite3
import json
from datetime import datetime

from config import DB_PATH, BACKUP_DIR, BACKUP_KEEP

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
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_result_batch ON recon_result(recon_no);
CREATE INDEX IF NOT EXISTS idx_result_status ON recon_result(status);
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
    add_cols("hist_price", {"quantity": "REAL", "total_amount": "REAL",
                            "bill_date": "TEXT", "remark": "TEXT"})
    conn.commit()
    conn.close()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_no(prefix):
    # 时间戳 + 进程号 + 4位随机，避免同一秒上传多个文件时批次号冲突
    import random
    return prefix + datetime.now().strftime("%Y%m%d%H%M%S") + str(os.getpid() % 100).zfill(2) + str(random.randint(0, 9999)).zfill(4)


# ---------- 历史导入（单事务聚合） ----------

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
        """SELECT supplier, model, model_norm, material, size, unit_price, period, exclude_reason
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
            g["prices"].append((r["period"], r["unit_price"]))

    import re
    for (supplier, norm), g in groups.items():
        prices = [(p, v) for p, v in g["prices"] if v is not None and v > 0]
        levels = sorted(set(round(v, 4) for _, v in prices))
        min_price = min(levels) if levels else None
        # 最新价：按 period 月份取最大
        def period_key(pv):
            p = pv[0] or ""
            m = re.search(r"(\d{4})[^\d]*(\d{1,2})", p)
            return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        latest = max(prices, key=period_key)[1] if prices else None
        detail = [{"period": p or "", "price": v} for p, v in prices]
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
            dedup_rows, seen = [], set()
            for r in rows:
                key = "|".join([r["supplier"], r["model_norm"], str(r["unit_price"]), r["period"]])
                h = hashlib.md5(key.encode("utf-8")).hexdigest()
                if h in seen:
                    continue
                seen.add(h)
                dedup_rows.append(_hist_insert_params(r, batch_no, h))
            n_inserted = _insert_hist_rows_conn(conn, dedup_rows)
            dup = len(rows) - n_inserted   # 文件内重复 + 跨批次重复
            conn.execute(
                "UPDATE import_batch SET total_rows=?,valid_rows=?,excluded_rows=?,error_rows=?,status=? WHERE batch_no=?",
                (meta.get("total", 0), meta.get("valid", 0), n_warn, dup, "完成", batch_no),
            )
            reports.append({"file": fname, "ok": True, "supplier": meta.get("supplier", ""),
                            "header_row": meta.get("header_row"), "sheet": meta.get("sheet", ""),
                            "total": meta.get("total", 0), "valid": meta.get("valid", 0),
                            "dup": dup, "batch_no": batch_no})
        idx_count = _rebuild_price_index_conn(conn)
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
            conn.execute(
                """INSERT INTO recon_result
                (recon_no,supplier,model,model_norm,material,size,bill_price,period,source_file,
                 source_row,status,lowest_price,evidence,reason,raw_values_json,created_at)
                VALUES(:recon_no,:supplier,:model,:model_norm,:material,:size,:bill_price,:period,:source_file,
                       :source_row,:status,:lowest_price,:evidence,:reason,:raw_values_json,:created_at)""",
                r,
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
    """清空全部业务数据（保留结构）并 VACUUM 回收磁盘空间。"""
    conn = connect()
    for t in ("recon_result", "recon_batch", "hist_price", "price_index", "import_batch"):
        conn.execute("DELETE FROM " + t)
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
