# -*- coding: utf-8 -*-
"""账单核对系统 · Flask 主程序。

两个入口：历史数据上传 / 新账单核对；结果查看与下载。数据全部本地存储。

本次重构（上线前复盘 A/B 组）：
- A-2 仅支持 .xlsx/.xlsm/.xls，移除 CSV/TSV 幻影支持声明；
- A-3 历史导入/核对入库改为单事务（db.import_history / db.save_recon_file），
  失败如实报错并整体回滚；
- A-7 管理员密码集中到 config；secret_key 随机生成持久化；登录失败锁定；
- B-2 全链路日志（logs/app.log，含异常堆栈）；
- B-3 清空历史库前自动备份（data/backups/）；
- B-5 移除未接线的 /api/histdb/* 孤岛路由（保留 hist_db 模块与表）；
- B-6 启动时按保留策略清理超期上传/导出文件。
"""
import os
import re
import json
import time
import secrets
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta

from flask import Flask, render_template, request, jsonify, send_file, session

import config
import db
import matcher
import exporter
import validator
import backup
import recon_stats

app = Flask(__name__)


# ---------- 静态资源版本号：取 style.css / app.js 的修改时间 ----------
# 改了前端却没生效，多半是浏览器用了缓存。模板里带 ?v=<mtime>，文件一变 URL 就变，
# 浏览器自然重新拉取；文件没变则继续命中缓存。
def _asset_ver():
    stamp = 0
    for name in ("style.css", "app.js"):
        try:
            stamp = max(stamp, int(os.path.getmtime(os.path.join(app.static_folder, name))))
        except OSError:
            pass
    return str(stamp or 1)


@app.context_processor
def _inject_asset_ver():
    return {"asset_ver": _asset_ver()}


# ---------- 会话密钥：随机生成一次并持久化（不随重启漂移，不可预测） ----------
def _load_secret_key():
    path = os.path.join(config.DATA_DIR, ".secret_key")
    if os.path.exists(path):
        with open(path) as f:
            key = f.read().strip()
        if key:
            return key
    key = secrets.token_hex(32)
    with open(path, "w") as f:
        f.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key

app.secret_key = _load_secret_key()
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100MB

# 清空历史库的权限密码（集中于 config，服务端校验，仅管理员持有）
CLEAR_PASSWORD = config.ADMIN_PASSWORD

# 仅支持 Excel（移除 CSV/TSV：解析器不支持，避免承诺与实现矛盾）
ALLOWED = (".xlsx", ".xlsm", ".xls")

# ---------- 日志：轮转文件 + 控制台 ----------
def _setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(os.path.join(config.LOG_DIR, "app.log"),
                             maxBytes=1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    return logging.getLogger("recon")

log = _setup_logging()

db.init_db()

# 型号归一化规则升级后，历史库里的 model_norm 需按新规则重算，
# 否则新账单核对时新旧键不一致会导致「历史里有却匹配不到」。
try:
    _changed, _idx = db.refresh_model_norms()
    if _changed:
        log.info("型号归一化迁移完成：重算 %d 条历史记录，价格基准 %d 个", _changed, _idx)
except Exception:
    log.exception("型号归一化迁移失败（不影响启动，但历史型号匹配可能不准）")

# 去重口径升级（v2）：时间与明细内容全部参与去重，同月同价的不同发货明细不再合并。
# 必须在 refresh_model_norms 之后（键里含 model_norm）；幂等。
try:
    _n = db.migrate_dedup_v2()
    if _n:
        log.info("去重口径升级完成：重算 %d 条历史记录的 dedup_hash", _n)
except Exception:
    log.exception("去重口径升级失败（不影响启动，但可能与旧口径数据重复）")

# 供应商历史有效数据台账：启动时对齐一次（幂等，指标未变的期间不会被改写、也不产生事件）
try:
    _h = db.rebuild_supplier_history(ref="初始化")
    if _h["changed"]:
        log.info("供应商历史台账初始化：%d 个供应商期间，新增事件 %d 条", _h["changed"], _h["events"])
except Exception:
    log.exception("供应商历史台账初始化失败（不影响启动，页面会在下次写入后刷新）")

# ---------- 管理员登录失败锁定（内存计数，重启清零） ----------
_login_fail = {"count": 0, "until": 0.0}


def is_admin_session():
    return session.get("admin") is True


def dumps_safe(obj):
    """JSON 序列化兜底：日期/未知对象转字符串，避免 datetime 等不可序列化导致入库失败。"""
    def conv(o):
        if hasattr(o, "isoformat"):
            return o.isoformat()
        return str(o)
    return json.dumps(obj, ensure_ascii=False, default=conv)


def allowed(filename):
    return filename.lower().endswith(ALLOWED)


def save_upload(file):
    name = os.path.basename(file.filename)
    # 每次上传使用独立子目录，避免同名文件互相覆盖、拒绝时便于清理
    sub = os.path.join(config.UPLOAD_DIR, db.new_no("U"))
    os.makedirs(sub, exist_ok=True)
    path = os.path.join(sub, name)
    file.save(path)
    return path


def cleanup_uploads(paths):
    """审查失败时清理本次上传的临时文件。"""
    for p in paths:
        try:
            os.remove(p)
            parent = os.path.dirname(p)
            if not os.listdir(parent):
                os.rmdir(parent)
        except OSError:
            pass


def housekeeping():
    """启动时按保留策略清理超期文件（uploads/outputs），并整理备份份数。"""
    cutoff = datetime.now() - timedelta(days=config.UPLOAD_RETENTION_DAYS)

    def expired(path):
        try:
            return datetime.fromtimestamp(os.path.getmtime(path)) < cutoff
        except OSError:
            return False

    n = 0
    for d in os.listdir(config.UPLOAD_DIR):
        sub = os.path.join(config.UPLOAD_DIR, d)
        if os.path.isdir(sub) and expired(sub):
            import shutil
            try:
                shutil.rmtree(sub)
                n += 1
            except OSError:
                pass
    for f in os.listdir(config.OUTPUT_DIR):
        p = os.path.join(config.OUTPUT_DIR, f)
        if os.path.isfile(p) and expired(p):
            try:
                os.remove(p)
                n += 1
            except OSError:
                pass
    if n:
        log.info("housekeeping: 已清理 %d 个超期文件/目录（保留 %d 天）", n, config.UPLOAD_RETENTION_DAYS)


@app.route("/")
def index():
    hist_rows, idx_count = db.hist_count()
    # 供应商从主数据读取（不再写死在模板里），新增后刷新页面即为最新
    try:
        suppliers = db.supplier_names()
    except Exception:
        suppliers = []
    return render_template("index.html", hist_rows=hist_rows, idx_count=idx_count,
                           suppliers=suppliers)


# ---------- 入口1：历史数据上传（先审查，全通过才入库） ----------
@app.route("/api/history/upload", methods=["POST"])
def api_history_upload():
    files = request.files.getlist("files")
    supplier_override = request.form.get("supplier", "").strip()
    if not files or all(not f.filename for f in files):
        return jsonify(ok=False, error="未选择文件"), 400

    # === 阶段一：全部文件严格审查，任一错误即整批拒绝，不写入任何数据 ===
    staged = []          # [(文件名, 路径, review)]
    all_errors, all_warnings = [], []
    saved_paths = []
    for f in files:
        if not f.filename or not allowed(f.filename):
            all_errors.append({"file": f.filename or "(空)", "row": 0, "field": "文件格式",
                               "value": f.filename or "",
                               "reason": "不支持的文件格式，仅支持 .xlsx / .xlsm / .xls（Excel）",
                               "cause": "如为 .csv 等其它格式，请先用 Excel 另存为 .xlsx 后重新上传。"})
            continue
        path = save_upload(f)
        saved_paths.append(path)
        review = validator.validate_file(path, supplier_override, "history")
        staged.append((f.filename, path, review))
        all_errors.extend(review["errors"])
        all_warnings.extend(review["warnings"])

    if all_errors:
        cleanup_uploads(saved_paths)
        return jsonify(ok=False,
                       error="数据审查未通过：共 %d 处问题，本次全部文件均未录入系统。请按下列错误修改后重新上传。"
                             % len(all_errors),
                       errors=all_errors, warnings=all_warnings,
                       rejected=[f.filename for f in files if f.filename]), 400

    # === 阶段二：全部通过，单事务原子写入（任一环节失败整体回滚） ===
    try:
        reports, added, hist_rows, idx_count = db.import_history(
            [(fname, review["rows"], review["meta"], len(review["warnings"]))
             for fname, _path, review in staged]
        )
    except Exception as e:
        log.exception("历史导入失败（已整体回滚）")
        return jsonify(ok=False,
                       error="入库失败，本次所有文件已整体回滚，数据库未发生任何变更：%s" % str(e),
                       errors=[{"file": "", "row": 0, "field": "系统", "value": "", "reason": str(e),
                                "cause": "详见服务端日志 logs/app.log；常见原因：文件损坏、并发写入冲突。"}]), 500

    log.info("历史导入成功：新增 %d 条，累计 %d 条，型号基准 %d 个", added, hist_rows, idx_count)
    return jsonify(ok=True, reports=reports, idx_count=idx_count, hist_rows=hist_rows,
                   added=added,
                   msg="追加成功：本次新增 %d 条，历史库累计 %d 条（原有数据完整保留，未被替换）" % (added, hist_rows),
                   warnings=all_warnings)


@app.route("/api/history/batches")
def api_history_batches():
    return jsonify(ok=True, batches=db.list_history_batches())


@app.route("/api/history/reset", methods=["POST"])
def api_history_reset():
    # 后端强制校验清空权限：无权限直接拒绝，不执行任何清空
    if not is_admin_session():
        log.warning("越权清空请求被拒绝（非管理员会话）")
        return jsonify(ok=False, error="无清空权限：仅管理员可执行清空历史库操作，请求已被拒绝"), 403
    # 清空前自动备份（数据安全网：误清空可从 backups/ 恢复）
    backup_path = ""
    try:
        backup_path = db.backup_db("pre_clear")
        db.clear_all()
    except Exception as e:
        log.exception("清空历史库失败")
        return jsonify(ok=False, error="清空失败：%s（历史数据未受影响）" % str(e)), 500
    log.info("历史库已清空（清空前备份：%s）", backup_path)
    return jsonify(ok=True, msg="已清空历史价格库与全部核对结果",
                   backup=backup_path, backup_note="清空前已自动备份，如需恢复请联系管理员使用 %s" % backup_path)


# ---------- 管理员权限（清空操作） ----------
@app.route("/api/auth/status")
def api_auth_status():
    return jsonify(ok=True, is_admin=is_admin_session())


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    now = time.time()
    data = request.get_json(silent=True) or {}
    pwd = str(data.get("password", ""))
    locked = _login_fail["until"] > now
    if pwd != CLEAR_PASSWORD:
        _login_fail["count"] += 1
        # 锁定期内：错误密码一律拒绝（防暴力破解），不给出任何试探窗口
        if locked:
            wait = int(_login_fail["until"] - now)
            log.warning("锁定期内的错误密码尝试被拒绝")
            return jsonify(ok=False,
                           error="登录已锁定，请 %d 秒后重试（锁定期间错误密码一律拒绝）" % wait), 423
        if _login_fail["count"] >= config.LOGIN_LOCK_AFTER:
            _login_fail["until"] = now + config.LOGIN_LOCK_SECONDS
            _login_fail["count"] = 0
            log.warning("管理员登录连续失败 %d 次，锁定 %d 秒", config.LOGIN_LOCK_AFTER, config.LOGIN_LOCK_SECONDS)
            return jsonify(ok=False,
                           error="登录失败次数过多，已锁定 %d 分钟，请稍后重试" % (config.LOGIN_LOCK_SECONDS // 60)), 423
        log.warning("管理员登录密码错误（第 %d 次）", _login_fail["count"])
        return jsonify(ok=False, error="管理员密码错误，未获得清空权限"), 403
    # 密码正确：即使此前处于锁定期也允许登录并清零计数
    # （避免管理员被自己锁死 10 分钟、只能重启服务的运维困境；防爆破不受影响）
    was_locked = locked
    _login_fail["count"] = 0
    _login_fail["until"] = 0.0
    session["admin"] = True
    log.info("管理员登录成功%s", "（此前处于锁定期，已通过正确密码解锁）" if was_locked else "")
    return jsonify(ok=True, msg="管理员身份验证通过，已获得清空历史库权限")


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    session.pop("admin", None)
    return jsonify(ok=True, msg="已退出管理员模式")


# ---------- 供应商主数据（可扩展：新增后下拉与校验名单立即生效） ----------
@app.route("/api/suppliers")
def api_suppliers():
    """供应商列表。with_usage=True 附带历史/核对引用数，供前端提示「已被使用不可删除」。"""
    try:
        return jsonify(ok=True, suppliers=db.list_suppliers(with_usage=True))
    except Exception as e:
        log.exception("供应商列表读取失败")
        return jsonify(ok=False, error="供应商列表读取失败：%s" % e), 500


@app.route("/api/suppliers", methods=["POST"])
def api_supplier_add():
    """新增供应商：写入 supplier 主数据后返回最新列表，前端据此刷新下拉。"""
    data = request.get_json(silent=True) or request.form
    name = str(data.get("name", "") or "").strip()
    alias = str(data.get("alias", "") or "").strip()
    remark = str(data.get("remark", "") or "").strip()
    try:
        sup = db.add_supplier(name, alias, remark)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    except Exception as e:
        log.exception("新增供应商失败")
        return jsonify(ok=False, error="保存失败：%s" % e), 500
    log.info("新增供应商：%s（别名：%s）", sup["name"], sup.get("alias") or "无")
    return jsonify(ok=True, supplier=sup, suppliers=db.list_suppliers(with_usage=True),
                   msg="供应商「%s」已保存，下拉选项与账单校验名单已同步更新" % sup["name"])


@app.route("/api/suppliers/<int:sid>/delete", methods=["POST"])
def api_supplier_delete(sid):
    """删除供应商档案（仅管理员；已被历史/核对引用的拒绝删除）。"""
    if not is_admin_session():
        return jsonify(ok=False, error="需要管理员权限才能删除供应商档案"), 403
    try:
        name = db.delete_supplier(sid)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    except Exception as e:
        log.exception("删除供应商失败")
        return jsonify(ok=False, error="删除失败：%s" % e), 500
    log.info("删除供应商：%s", name)
    return jsonify(ok=True, suppliers=db.list_suppliers(with_usage=True),
                   msg="供应商「%s」已删除" % name)


@app.route("/api/suppliers/history")
def api_supplier_history():
    """供应商历史有效数据：按供应商 + 月份范围查询（含状态、来源、指标与最近变化）。"""
    supplier = (request.args.get("supplier") or "").strip()
    month_from = (request.args.get("month_from") or "").strip()
    month_to = (request.args.get("month_to") or "").strip()
    include_expired = (request.args.get("include_expired") or "1") != "0"
    for m in (month_from, month_to):
        if m and not re.match(r"^\d{4}-\d{2}$", m):
            return jsonify(ok=False, error="月份格式应为 YYYY-MM（如 2026-07）"), 400
    if month_from and month_to and month_from > month_to:
        return jsonify(ok=False, error="起始月份不能晚于结束月份"), 400
    try:
        rows, models = db.list_supplier_history(supplier, month_from, month_to, include_expired)
        events = db.list_supplier_events(supplier or None, limit=40)
    except Exception as e:
        log.exception("供应商历史查询失败")
        return jsonify(ok=False, error="查询失败：%s" % e), 500

    # 汇总只统计有效期间（rows_valid>0），失效期间不计入有效记录
    valid = [r for r in rows if (r.get("rows_valid") or 0) > 0]
    prices = [r["min_price"] for r in valid if r.get("min_price") is not None]
    summary = {
        "periods": len(valid),
        "periods_expired": len(rows) - len(valid),
        "rows_valid": sum(r["rows_valid"] or 0 for r in valid),
        "models": models,
        "amount_total": round(sum(r["amount_total"] or 0 for r in valid), 2),
        "min_price": min(prices) if prices else None,
        "last_period": valid[-1]["period"] if valid else "",
        "updated_at": max([r["updated_at"] or "" for r in rows], default=""),
        "suppliers": sorted({r["supplier"] for r in valid}),
    }
    return jsonify(ok=True, rows=rows, events=events, summary=summary,
                   labels=db.SUP_HIST_LABELS)


@app.route("/api/suppliers/history/rebuild", methods=["POST"])
def api_supplier_history_rebuild():
    """手动重建台账（管理员，用于修复或口径调整后重算）。"""
    if not is_admin_session():
        return jsonify(ok=False, error="需要管理员权限才能重建供应商历史台账"), 403
    try:
        r = db.rebuild_supplier_history(ref="手动重建")
    except Exception as e:
        log.exception("供应商历史台账重建失败")
        return jsonify(ok=False, error="重建失败：%s" % e), 500
    return jsonify(ok=True, changed=r["changed"], events=r["events"],
                   msg="台账已重建：更新 %d 个供应商期间，新增状态变化 %d 条" % (r["changed"], r["events"]))


# ---------- 账单备份：按条件筛选 → 打包下载（可归档、可回灌） ----------

def _backup_filters(args):
    """解析备份筛选条件（GET 查询串）：账单类型 / 供应商 / 是否含无效价行。

    导出粒度固定为「按供应商分类 + 全部期间」，不再支持按月筛选。
    """
    months = [k for k in ("month_from", "month_to") if (args.get(k) or "").strip()]
    if months:
        raise ValueError("账单导出已取消按月筛选，仅支持按供应商导出全部历史记录（请去掉 month_from / month_to 参数）")
    kinds = [k for k in (args.get("kinds") or "").split(",") if k in ("history", "recon")]
    if not kinds:
        kinds = ["history"]
    suppliers = [s.strip() for s in (args.get("suppliers") or "").split(",") if s.strip()]
    return {
        "kinds": kinds,
        "suppliers": suppliers,
        "include_excluded": (args.get("include_excluded") or "") in ("1", "true", "on"),
    }


# ---------- 账单导出/备份：仅管理员 ----------
BACKUP_DENIED = "账单导出与备份仅管理员可用，请先以管理员身份登录后再操作"


def _require_admin_for_backup():
    """未登录管理员时返回 403 响应，否则返回 None。"""
    if not is_admin_session():
        return jsonify(ok=False, error=BACKUP_DENIED), 403
    return None


@app.route("/api/history/backup/preview")
def api_backup_preview():
    """导出前预览：将生成多少文件、多少行、金额合计，避免导出一个空包。（仅管理员）"""
    denied = _require_admin_for_backup()
    if denied:
        return denied
    try:
        filters = _backup_filters(request.args)
        files, _groups = backup.plan(filters)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, files=files,
                   totals={"files": len(files),
                           "rows": sum(f["rows"] for f in files),
                           "amount": round(sum(f["amount"] for f in files), 2),
                           "history_rows": sum(f["rows"] for f in files if f["kind"] == "history"),
                           "recon_rows": sum(f["rows"] for f in files if f["kind"] == "recon")},
                   scope=backup.SCOPE_TEXT,
                   zip_name=backup.zip_name(filters, files))


@app.route("/api/history/backup")
def api_backup_download():
    """按条件打包下载（ZIP：分表 + 备份清单.csv/json + 恢复说明.txt）。（仅管理员）"""
    denied = _require_admin_for_backup()
    if denied:
        return denied
    try:
        filters = _backup_filters(request.args)
        path, manifest = backup.build(filters)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    except Exception as e:
        log.exception("账单备份打包失败")
        return jsonify(ok=False, error="备份打包失败：%s" % e), 500
    log.info("账单备份：%s（%d 个文件 / %d 条记录）", os.path.basename(path),
             manifest["totals"]["files"], manifest["totals"]["rows"])
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


# ---------- 入口2：新账单核对 ----------
@app.route("/api/reconcile", methods=["POST"])
def api_reconcile():
    files = request.files.getlist("files")
    supplier_override = request.form.get("supplier", "").strip()
    if not files or all(not f.filename for f in files):
        return jsonify(ok=False, error="未选择文件"), 400

    hist_rows, idx_count = db.hist_count()
    if hist_rows == 0 or idx_count == 0:
        return jsonify(ok=False, error="历史价格库为空，请先在「历史数据上传」导入历史账单"), 400

    # === 阶段一：严格审查，任一错误即整批拒绝 ===
    staged, all_errors, all_warnings, saved_paths = [], [], [], []
    for f in files:
        if not f.filename or not allowed(f.filename):
            all_errors.append({"file": f.filename or "(空)", "row": 0, "field": "文件格式",
                               "value": f.filename or "",
                               "reason": "不支持的文件格式，仅支持 .xlsx / .xlsm / .xls（Excel）",
                               "cause": "如为 .csv 等其它格式，请先用 Excel 另存为 .xlsx 后重新上传。"})
            continue
        path = save_upload(f)
        saved_paths.append(path)
        review = validator.validate_file(path, supplier_override, "pending")
        staged.append((f.filename, review))
        all_errors.extend(review["errors"])
        all_warnings.extend(review["warnings"])
    if all_errors:
        cleanup_uploads(saved_paths)
        return jsonify(ok=False,
                       error="数据审查未通过：共 %d 处问题，本次核对未执行、未写入任何数据。"
                             % len(all_errors),
                       errors=all_errors, warnings=all_warnings), 400

    # === 阶段二：逐文件核对并单事务入库（单文件失败即回滚该批次，不影响其它文件） ===
    batches, all_counts = [], {"total": 0, "matched": 0, "review": 0, "above": 0,
                               "nohistory": 0, "error": 0, "diff": 0,
                               "overpay": 0.0, "underpay": 0.0}
    for fname, review in staged:
        recon_no = db.new_no("R")
        rows = review["rows"]
        for r in rows:
            r["exclude_reason"] = None
        results, counts = matcher.run_match(rows)
        for r in results:
            r["recon_no"] = recon_no
            # 按上传表头保存原始行值，供核对明细原样输出
            r["raw_values_json"] = dumps_safe(r.get("raw_values", []))
        try:
            db.save_recon_file(recon_no, fname,
                               headers=review["meta"].get("headers", []),
                               mapping_diff=review["meta"].get("mapping_diff", []),
                               results=results, counts=counts)
        except Exception as e:
            log.exception("核对入库失败（批次 %s 已回滚）", recon_no)
            return jsonify(ok=False,
                           error="核对入库失败：文件「%s」已回滚，之前完成的核对批次不受影响：%s"
                                 % (fname, str(e))), 500
        for k in all_counts:
            all_counts[k] += counts[k]
        batches.append({"file": fname, "ok": True, "recon_no": recon_no,
                        "supplier": review["meta"]["supplier"],
                        "total_rows": counts["total"], "matched": counts["matched"],
                        "review": counts["review"], "above": counts["above"],
                        "nohistory": counts["nohistory"], "error": counts["error"],
                        "diff": counts["diff"], "overpay": counts["overpay"],
                        "underpay": counts["underpay"]})
        log.info("核对完成：%s 批次 %s 总 %d / 匹配 %d / 复核 %d / 高于 %d / 无历史 %d / 解析失败 %d / 差异 %d 笔 超付 %.2f",
                 fname, recon_no, counts["total"], counts["matched"], counts["review"],
                 counts["above"], counts["nohistory"], counts["error"], counts["diff"],
                 counts["overpay"])

    return jsonify(ok=True, batches=batches, counts=all_counts,
                   total_results=sum(all_counts.values()), warnings=all_warnings)


@app.route("/api/reconcile/batches")
def api_reconcile_batches():
    return jsonify(ok=True, batches=db.list_recon_batches())


@app.route("/api/results/<recon_no>")
def api_results(recon_no):
    batch = db.get_recon_batch(recon_no)
    if not batch:
        return jsonify(ok=False, error="核对批次不存在"), 404
    status = request.args.get("status")
    diff = request.args.get("diff")     # 差异类型筛选（如 amt_high / base_stale）
    results = db.get_results(recon_no, status)
    if diff:
        results = [r for r in results if diff in str(r.get("diff_types") or "").split(",")]
    # 解析上传表头与每行原始值，供前端按上传表头展示核对明细
    try:
        headers = json.loads(batch.get("headers_json") or "[]")
    except Exception:
        headers = []
    for r in results:
        try:
            r["raw_values"] = json.loads(r.get("raw_values_json") or "[]")
        except Exception:
            r["raw_values"] = []
        r.pop("raw_values_json", None)
        r["diff_list"] = [c for c in str(r.get("diff_types") or "").split(",") if c]
    batch_out = dict(batch)
    batch_out.pop("headers_json", None)
    batch_out.pop("mapping_diff_json", None)
    # 统计口径统一由 recon_stats 计算（页面与 Excel 共用一套），且永远基于整批而非筛选结果
    summary = recon_stats.summarize(db.get_results(recon_no))
    return jsonify(ok=True, batch=batch_out, results=results, headers=headers, summary=summary)


@app.route("/api/results/<recon_no>/summary")
def api_results_summary(recon_no):
    """仅取统计（筛选/刷新时避免重复拉明细）。"""
    if not db.get_recon_batch(recon_no):
        return jsonify(ok=False, error="核对批次不存在"), 404
    return jsonify(ok=True, summary=recon_stats.summarize(db.get_results(recon_no)))


@app.route("/api/download/<recon_no>")
def api_download(recon_no):
    batch = db.get_recon_batch(recon_no)
    if not batch:
        return jsonify(ok=False, error="核对批次不存在"), 404
    results = db.get_results(recon_no)
    path = exporter.export_results(recon_no, results, batch)
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


@app.route("/api/status")
def api_status():
    hist_rows, idx_count = db.hist_count()
    return jsonify(ok=True, hist_rows=hist_rows, idx_count=idx_count)


@app.errorhandler(413)
def too_large(e):
    return jsonify(ok=False, error="上传文件超过 100MB 限制"), 413


# ---------- 请求访问日志 ----------
@app.before_request
def _mark_start():
    import time
    request._start_ms = time.time() * 1000


@app.after_request
def _log_request(resp):
    try:
        cost = int(getattr(request, "_start_ms", 0) and (time.time() * 1000 - request._start_ms) or 0)
    except Exception:
        cost = 0
    log.info("%s %s -> %d (%d ms)", request.method, request.path, resp.status_code, cost)
    return resp


if __name__ == "__main__":
    import socket

    def lan_ip():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    housekeeping()
    ip = lan_ip()
    print("=" * 56)
    print("账单核对系统已启动（监听 0.0.0.0:5088，支持局域网访问）")
    print("  本机访问:    http://127.0.0.1:5088")
    print("  局域网访问:  http://%s:5088" % ip)
    print("  日志文件:    logs/app.log")
    print("  自动备份:    data/backups/（清空历史库前自动备份）")
    print("=" * 56)
    app.run(host="0.0.0.0", port=5088, debug=False, threaded=True)
