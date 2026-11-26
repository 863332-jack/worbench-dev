# -*- coding: utf-8 -*-
"""历史数据库管理：追加式版本记录、软删除保护、多条件匹配查询。

设计原则：
1. 只追加不覆盖：更新=新增一条更高版本号的记录，原记录永不修改；
2. 默认不可删除：删除需管理员密码校验，通过后才置删除状态（软删除，记录仍保留）；
3. 匹配查询：日期、型号、材质同时满足（AND），结果按记录ID去重；
4. 字段完整：记录ID/业务ID/日期/型号/材质/数据内容/来源/版本号/创建时间/操作人/删除状态。
"""
import json
from datetime import datetime

import db
from config import ADMIN_PASSWORD

# 删除操作管理员密码：统一取自 config.ADMIN_PASSWORD（原独立硬编码已收敛，避免两处漂移）
# 注意：/api/histdb/* 路由已按上线前复盘 B-5 移除（孤岛 API），本模块保留为
# 内部数据层备用，未接入 HTTP 路由。
ADMIN_DELETE_PASSWORD = ADMIN_PASSWORD

FIELDS = [
    "id", "biz_id", "rec_date", "model", "material", "content",
    "source", "version", "created_at", "operator", "deleted",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS hist_record (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,   -- 记录ID（系统唯一）
    biz_id      TEXT NOT NULL,                        -- 业务ID（业务唯一键，同键多版本）
    rec_date    TEXT,                                 -- 日期 YYYY-MM-DD
    model       TEXT,                                 -- 型号
    material    TEXT,                                 -- 材质
    content     TEXT,                                 -- 数据内容/参数（JSON 或文本）
    source      TEXT,                                 -- 来源
    version     INTEGER NOT NULL DEFAULT 1,           -- 版本号（同 biz_id 递增）
    created_at  TEXT,                                 -- 创建时间
    operator    TEXT,                                 -- 操作人
    deleted     INTEGER DEFAULT 0,                    -- 删除状态 0=有效 1=已删除
    deleted_at  TEXT,                                 -- 删除时间
    deleted_by  TEXT                                  -- 删除操作人
);
CREATE INDEX IF NOT EXISTS idx_hist_rec_biz ON hist_record(biz_id, version);
CREATE INDEX IF NOT EXISTS idx_hist_rec_match ON hist_record(rec_date, model, material);
CREATE INDEX IF NOT EXISTS idx_hist_rec_deleted ON hist_record(deleted);
"""


def init():
    conn = db.connect()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def verify_delete_password(password):
    """校验管理员密码。返回 (bool, 错误信息)。"""
    if password is None or str(password) == "":
        return False, "删除历史记录必须输入管理员密码"
    if str(password).strip() != ADMIN_DELETE_PASSWORD:
        return False, "管理员密码错误，删除操作已被拒绝，记录未发生任何变更"
    return True, ""


def append_record(biz_id, rec_date, model, material, content, source, operator):
    """追加一条历史记录：始终 INSERT，不覆盖任何已有记录。

    version = 同一 biz_id 下最大版本号 + 1。
    返回新增记录 dict。
    """
    biz_id = str(biz_id or "").strip()
    if not biz_id:
        raise ValueError("业务ID不能为空")
    conn = db.connect()
    max_v = conn.execute(
        "SELECT COALESCE(MAX(version),0) FROM hist_record WHERE biz_id=?", (biz_id,)
    ).fetchone()[0]
    version = int(max_v) + 1
    if isinstance(content, (dict, list)):
        content = json.dumps(content, ensure_ascii=False)
    cur = conn.execute(
        """INSERT INTO hist_record
        (biz_id,rec_date,model,material,content,source,version,created_at,operator,deleted)
        VALUES(?,?,?,?,?,?,?,?,?,0)""",
        (biz_id, rec_date or "", model or "", material or "", content or "",
         source or "", version, now(), operator or ""),
    )
    rid = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM hist_record WHERE id=?", (rid,)).fetchone()
    conn.close()
    return dict(row)


def query_records(rec_date=None, model=None, material=None, biz_id=None,
                  include_deleted=False, latest_only=False):
    """多条件匹配查询：日期、型号、材质作为同时满足的 AND 条件。

    结果按记录ID去重（SQL DISTINCT id，主键本身唯一，防止关联/重复写入造成重复）。
    latest_only=True 时每个业务ID只保留最高版本。
    """
    where, args = [], []
    if not include_deleted:
        where.append("deleted=0")
    if rec_date:
        where.append("rec_date=?"); args.append(str(rec_date))
    if model:
        where.append("model=?"); args.append(str(model))
    if material:
        where.append("material=?"); args.append(str(material))
    if biz_id:
        where.append("biz_id=?"); args.append(str(biz_id))
    sql = "SELECT DISTINCT id,biz_id,rec_date,model,material,content,source,version,created_at,operator,deleted FROM hist_record"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY biz_id, version DESC, id DESC"
    conn = db.connect()
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    out = [dict(r) for r in rows]
    if latest_only:
        seen, uniq = set(), []
        for r in out:
            if r["biz_id"] in seen:
                continue
            seen.add(r["biz_id"])
            uniq.append(r)
        return uniq
    return out


def get_versions(biz_id):
    """列出某业务ID的全部版本（含已删除，用于版本追溯）。"""
    conn = db.connect()
    rows = conn.execute(
        "SELECT * FROM hist_record WHERE biz_id=? ORDER BY version", (biz_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_record(record_id, password, operator=""):
    """删除记录：先校验管理员密码，失败则拒绝且不改动任何数据。

    采用软删除（置 deleted=1），记录本体永久保留，符合历史数据保护要求。
    返回 (ok, message, record)。
    """
    ok, msg = verify_delete_password(password)
    if not ok:
        return False, msg, None
    conn = db.connect()
    row = conn.execute("SELECT * FROM hist_record WHERE id=?", (record_id,)).fetchone()
    if not row:
        conn.close()
        return False, "记录ID不存在，未执行删除", None
    if row["deleted"]:
        conn.close()
        return False, "该记录已是删除状态，无需重复删除", dict(row)
    conn.execute(
        "UPDATE hist_record SET deleted=1, deleted_at=?, deleted_by=? WHERE id=?",
        (now(), operator or "", record_id),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM hist_record WHERE id=?", (record_id,)).fetchone()
    conn.close()
    return True, "删除成功：记录已标记为删除状态（记录本体保留，可追溯）", dict(updated)


def stats():
    conn = db.connect()
    total = conn.execute("SELECT COUNT(*) FROM hist_record").fetchone()[0]
    active = conn.execute("SELECT COUNT(*) FROM hist_record WHERE deleted=0").fetchone()[0]
    deleted = conn.execute("SELECT COUNT(*) FROM hist_record WHERE deleted=1").fetchone()[0]
    biz = conn.execute("SELECT COUNT(DISTINCT biz_id) FROM hist_record").fetchone()[0]
    conn.close()
    return {"total": total, "active": active, "deleted": deleted, "biz_ids": biz}
