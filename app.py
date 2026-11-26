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
import po_db

app = Flask(__name__)

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
po_db.init()

# 型号归一化规则升级后，历史库里的 model_norm 需要按新规则重算，
# 否则采购端用新键匹配旧数据会全部落空（表现为「历史里有却显示未找到」）。
try:
    _changed, _idx = db.refresh_model_norms()
    if _changed:
        log.info("型号归一化迁移完成：重算 %d 条历史记录，价格基准 %d 个", _changed, _idx)
except Exception:
    log.exception("型号归一化迁移失败（不影响启动，但历史型号匹配可能不准）")

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
    return render_template("index.html", hist_rows=hist_rows, idx_count=idx_count,
                           doc_exts=list(po_db.DOC_EXTS), doc_accept=po_db.DOC_ACCEPT)


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
    batches, all_counts = [], {"total": 0, "matched": 0, "review": 0, "above": 0, "nohistory": 0, "error": 0}
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
                        "nohistory": counts["nohistory"], "error": counts["error"]})
        log.info("核对完成：%s 批次 %s 总 %d / 匹配 %d / 复核 %d / 高于 %d / 无历史 %d / 解析失败 %d",
                 fname, recon_no, counts["total"], counts["matched"], counts["review"],
                 counts["above"], counts["nohistory"], counts["error"])

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
    results = db.get_results(recon_no, request.args.get("status"))
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
    batch_out = dict(batch)
    batch_out.pop("headers_json", None)
    batch_out.pop("mapping_diff_json", None)
    return jsonify(ok=True, batch=batch_out, results=results, headers=headers)


@app.route("/api/download/<recon_no>")
def api_download(recon_no):
    batch = db.get_recon_batch(recon_no)
    if not batch:
        return jsonify(ok=False, error="核对批次不存在"), 404
    results = db.get_results(recon_no)
    path = exporter.export_results(recon_no, results, batch)
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


# ---------- 采购工程流程：账号 / 下单 / 确认 / 收货 / 追踪 ----------
def po_session():
    return {"role": session.get("po_role"), "username": session.get("po_username"),
            "supplier": session.get("po_supplier"), "display": session.get("po_display")}


def po_require(role=None):
    """采购流程登录与角色校验。返回 (session_dict, error_response)。"""
    s = po_session()
    if not s["role"]:
        return None, (jsonify(ok=False, error="请先登录采购工程流程"), 401)
    if role and s["role"] != role:
        label = "采购方" if role == po_db.ROLE_PURCHASER else "供应商"
        return None, (jsonify(ok=False, error="当前账号无此操作权限（需要%s角色）" % label), 403)
    return s, None


def _po_file_ok(filename):
    return os.path.splitext(filename or "")[1].lower() in po_db.DOC_EXTS


def save_po_file(order_no, file, prefix):
    """保存图纸/收货凭证到 uploads/po/<order_no>/，返回 (原始文件名, 相对路径)。"""
    name = os.path.basename(file.filename or "")
    ext = os.path.splitext(name)[1].lower()
    safe = "%s_%s%s" % (prefix, datetime.now().strftime("%Y%m%d%H%M%S"), ext)
    sub = os.path.join(config.PO_FILE_DIR, order_no)
    os.makedirs(sub, exist_ok=True)
    path = os.path.join(sub, safe)
    file.save(path)
    return name, os.path.join(order_no, safe)


def _po_num(v, field, required=False, positive=True):
    """数量/单价解析：返回 (值, 错误信息)。"""
    s = str(v if v is not None else "").strip()
    if not s:
        return (None, "「%s」不能为空" % field) if required else (None, None)
    try:
        n = float(s)
    except ValueError:
        return None, "「%s」必须是数字" % field
    if n < 0:
        return None, "「%s」不能为负数" % field
    if positive and n == 0:
        return None, "「%s」必须大于 0" % field
    return n, None


@app.route("/api/po/login", methods=["POST"])
def api_po_login():
    data = request.get_json(silent=True) or {}
    user = po_db.verify_user(data.get("username", ""), data.get("password", ""))
    if not user:
        log.warning("采购流程登录失败：%s", data.get("username"))
        return jsonify(ok=False, error="账号或密码错误"), 403
    session["po_role"] = user["role"]
    session["po_username"] = user["username"]
    session["po_supplier"] = user["supplier"]
    session["po_display"] = user["display_name"]
    log.info("采购流程登录成功：%s（%s）", user["username"], user["role"])
    return jsonify(ok=True, user=user, msg="登录成功")


@app.route("/api/po/logout", methods=["POST"])
def api_po_logout():
    for k in ("po_role", "po_username", "po_supplier", "po_display"):
        session.pop(k, None)
    return jsonify(ok=True, msg="已退出采购流程")


@app.route("/api/po/me")
def api_po_me():
    s = po_session()
    if not s["role"]:
        return jsonify(ok=False, error="未登录"), 401
    return jsonify(ok=True, user={"username": s["username"], "role": s["role"],
                                  "supplier": s["supplier"], "display_name": s["display"]},
                   stats=po_db.stats())


@app.route("/api/po/model-lookup")
def api_po_model_lookup():
    """按「供应商 + 型号」匹配历史账单，返回回填数据（参数/尺寸/材料/单价）与匹配状态。

    供应商可选范围取自历史库实际出现的供应商（po_db.list_suppliers），
    不再硬编码 —— 历史数据里换了供应商写法也不会被前端挡住。
    """
    s, err = po_require(po_db.ROLE_PURCHASER)
    if err:
        return err
    supplier = (request.args.get("supplier") or "").strip()
    model = (request.args.get("model") or "").strip()
    if not supplier:
        return jsonify(ok=False, error="请先选择供应商，再按型号匹配历史"), 400
    if not model:
        return jsonify(ok=False, error="型号不能为空"), 400
    # 供应商不在历史库中时不直接报错：交给 lookup_model 返回「未找到 + 跨供应商诊断」，
    # 前端照常显示可操作提示，不会因校验拦截而丢失诊断信息。
    return jsonify(ok=True, **po_db.lookup_model(supplier, model))


@app.route("/api/po/suppliers")
def api_po_suppliers():
    """可选供应商列表（内置 + 历史库实际出现的），供下单页下拉与匹配校验共用。"""
    s, err = po_require()
    if err:
        return err
    return jsonify(ok=True, suppliers=po_db.list_suppliers())


@app.route("/api/po/orders", methods=["POST"])
def api_po_create_order():
    """多行下单：items_json（1-50 行）+ 行级图纸 drawing_<行号>。"""
    s, err = po_require(po_db.ROLE_PURCHASER)
    if err:
        return err
    form = request.form
    supplier = (form.get("supplier") or "").strip()
    remark = (form.get("remark") or "").strip()
    try:
        items = json.loads(form.get("items_json") or "[]")
    except Exception:
        return jsonify(ok=False, error="产品行数据格式错误，请刷新页面重试"), 400
    if not isinstance(items, list):
        return jsonify(ok=False, error="产品行数据格式错误"), 400
    if len(items) == 0:
        return jsonify(ok=False, error="请至少添加 1 行产品", errors=["产品表格为空"]), 400
    if len(items) > po_db.MAX_LINES:
        return jsonify(ok=False,
                       error="单次最多添加 %d 行产品，当前 %d 行，请分批下单"
                             % (po_db.MAX_LINES, len(items)),
                       errors=["产品行数超限（上限 %d 行）" % po_db.MAX_LINES]), 400

    errors = []
    allowed_suppliers = po_db.list_suppliers()
    if supplier not in allowed_suppliers:
        errors.append("「供应商」必须选择：%s" % " / ".join(allowed_suppliers))

    clean_items = []
    has_drawing = False
    for idx, it in enumerate(items):
        line = idx + 1
        if not isinstance(it, dict):
            errors.append("第 %d 行：数据格式错误" % line)
            continue
        model = str(it.get("model") or "").strip()
        if not model:
            errors.append("第 %d 行：「型号」不能为空" % line)
        qty, e = _po_num(it.get("quantity"), "数量", required=True)
        if e:
            errors.append("第 %d 行：%s" % (line, e))
        price, e = _po_num(it.get("unit_price"), "单价", required=False)
        if e:
            errors.append("第 %d 行：%s" % (line, e))
        rdate = str(it.get("required_date") or "").strip()
        if not rdate:
            errors.append("第 %d 行：「交货日期」不能为空" % line)
        else:
            try:
                datetime.strptime(rdate, "%Y-%m-%d")
            except ValueError:
                errors.append("第 %d 行：「交货日期」格式应为 YYYY-MM-DD" % line)
        f = request.files.get("drawing_%d" % line)
        if f and f.filename:
            if not _po_file_ok(f.filename):
                errors.append("第 %d 行：图纸格式不支持，允许：%s" % (line, " ".join(po_db.DOC_EXTS)))
            else:
                has_drawing = True
        clean_items.append({
            "line_no": line,
            "model": model,
            "param": str(it.get("param") or "").strip(),
            "size": str(it.get("size") or "").strip(),
            "material": str(it.get("material") or "").strip(),
            "quantity": qty,
            "unit": str(it.get("unit") or "").strip(),
            "unit_price": price,
            "required_date": rdate,
            "remark": str(it.get("remark") or "").strip(),
            "drawing_name": "", "drawing_path": "",
        })
    if not has_drawing:
        errors.append("请至少为一行产品上传图纸文件")
    if errors:
        return jsonify(ok=False, error="表单校验未通过，订单未创建", errors=errors), 400

    order_no = po_db.new_order_no()
    try:
        for it in clean_items:
            f = request.files.get("drawing_%d" % it["line_no"])
            if f and f.filename:
                it["drawing_name"], it["drawing_path"] = save_po_file(
                    order_no, f, "line%d" % it["line_no"])
        order = po_db.create_order_multi(supplier, s["display"] or s["username"],
                                         remark, clean_items, order_no=order_no)
    except Exception as e:
        log.exception("创建采购订单失败")
        import shutil
        shutil.rmtree(os.path.join(config.PO_FILE_DIR, order_no), ignore_errors=True)
        return jsonify(ok=False, error="创建订单失败：%s" % str(e)), 500
    log.info("采购下单成功：%s 供应商 %s 共 %d 行，总数量 %g", order_no, supplier,
             len(clean_items), order["quantity"])
    return jsonify(ok=True, order=order, msg="下单成功，订单号 %s（%d 行产品），等待供应商确认接单"
                                             % (order_no, len(clean_items)))


@app.route("/api/po/orders")
def api_po_orders():
    s, err = po_require()
    if err:
        return err
    status = request.args.get("status") or None
    if status and status not in po_db.STATUS_LABELS:
        return jsonify(ok=False, error="状态参数无效"), 400
    rows = po_db.list_orders(s["role"], s["supplier"], status)
    return jsonify(ok=True, orders=rows, stats=po_db.stats(),
                   labels=po_db.STATUS_LABELS, role=s["role"])


@app.route("/api/po/orders/<order_no>")
def api_po_order_detail(order_no):
    s, err = po_require()
    if err:
        return err
    order = po_db.get_order(order_no)
    if not order:
        return jsonify(ok=False, error="订单不存在"), 404
    if s["role"] == po_db.ROLE_SUPPLIER and order["supplier"] != s["supplier"]:
        return jsonify(ok=False, error="无权查看其他供应商的订单"), 403
    acts = []
    if s["role"] == po_db.ROLE_SUPPLIER and order["status"] == po_db.STATUS_PENDING:
        acts = ["confirm", "reject"]
    if s["role"] == po_db.ROLE_PURCHASER and order["status"] == po_db.STATUS_CONFIRMED:
        acts = ["receive"]
    return jsonify(ok=True, order=order, items=po_db.get_items(order_no),
                   events=po_db.get_events(order_no),
                   actions=acts, labels=po_db.STATUS_LABELS)


@app.route("/api/po/orders/<order_no>/confirm", methods=["POST"])
def api_po_confirm(order_no):
    s, err = po_require(po_db.ROLE_SUPPLIER)
    if err:
        return err
    order = po_db.get_order(order_no)
    if not order:
        return jsonify(ok=False, error="订单不存在"), 404
    if order["supplier"] != s["supplier"]:
        return jsonify(ok=False, error="无权操作其他供应商的订单"), 403
    try:
        order = po_db.confirm_order(order_no, s["display"] or s["username"])
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 409
    log.info("供应商确认接单：%s by %s", order_no, s["username"])
    return jsonify(ok=True, order=order, msg="已确认接单，等待采购方登记收货")


@app.route("/api/po/orders/<order_no>/reject", methods=["POST"])
def api_po_reject(order_no):
    s, err = po_require(po_db.ROLE_SUPPLIER)
    if err:
        return err
    order = po_db.get_order(order_no)
    if not order:
        return jsonify(ok=False, error="订单不存在"), 404
    if order["supplier"] != s["supplier"]:
        return jsonify(ok=False, error="无权操作其他供应商的订单"), 403
    data = request.get_json(silent=True) or {}
    reason = str(data.get("reason", "")).strip()
    if len(reason) > 200:
        return jsonify(ok=False, error="拒接原因不能超过 200 字"), 400
    try:
        order = po_db.reject_order(order_no, reason, s["display"] or s["username"])
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 409
    log.info("供应商拒接：%s by %s", order_no, s["username"])
    return jsonify(ok=True, order=order, msg="已拒接该订单")


@app.route("/api/po/orders/<order_no>/receive", methods=["POST"])
def api_po_receive(order_no):
    s, err = po_require(po_db.ROLE_PURCHASER)
    if err:
        return err
    order = po_db.get_order(order_no)
    if not order:
        return jsonify(ok=False, error="订单不存在"), 404
    form = request.form
    received_date = (form.get("received_date") or "").strip()
    remark = (form.get("receive_remark") or "").strip()
    receipt = request.files.get("receipt")

    errors = []
    qty, e = _po_num(form.get("received_qty"), "到货数量", required=True)
    if e:
        errors.append(e)
    if not received_date:
        errors.append("「到货日期」不能为空")
    else:
        try:
            datetime.strptime(received_date, "%Y-%m-%d")
        except ValueError:
            errors.append("「到货日期」格式应为 YYYY-MM-DD")
    if receipt and receipt.filename and not _po_file_ok(receipt.filename):
        errors.append("收货凭证格式不支持，允许：%s" % " ".join(po_db.DOC_EXTS))
    if errors:
        return jsonify(ok=False, error="表单校验未通过，收货未登记", errors=errors), 400

    receipt_name = receipt_path = ""
    if receipt and receipt.filename:
        try:
            receipt_name, receipt_path = save_po_file(order_no, receipt, "receipt")
        except Exception as e:
            return jsonify(ok=False, error="收货凭证保存失败：%s" % str(e)), 500
    try:
        order = po_db.receive_order(order_no, qty, received_date, remark,
                                    receipt_name, receipt_path, s["display"] or s["username"])
    except ValueError as e:
        if receipt_path:
            try:
                os.remove(os.path.join(config.PO_FILE_DIR, receipt_path))
            except OSError:
                pass
        return jsonify(ok=False, error=str(e)), 409
    log.info("采购方登记收货：%s 数量 %g by %s", order_no, qty, s["username"])
    return jsonify(ok=True, order=order, msg="收货登记成功，订单流程已完成")


@app.route("/api/po/orders/<order_no>/file")
def api_po_file(order_no):
    s, err = po_require()
    if err:
        return err
    order = po_db.get_order(order_no)
    if not order:
        return jsonify(ok=False, error="订单不存在"), 404
    if s["role"] == po_db.ROLE_SUPPLIER and order["supplier"] != s["supplier"]:
        return jsonify(ok=False, error="无权下载其他供应商订单的附件"), 403
    kind = request.args.get("type", "drawing")
    line = request.args.get("line")
    if kind == "drawing":
        if line:
            # 行级图纸：type=drawing&line=<行号>
            try:
                ln = int(line)
            except (TypeError, ValueError):
                return jsonify(ok=False, error="行号参数无效"), 400
            item = next((x for x in po_db.get_items(order_no) if x["line_no"] == ln), None)
            if not item:
                return jsonify(ok=False, error="订单不存在该产品行"), 404
            rel, shown = item.get("drawing_path"), item.get("drawing_name")
        else:
            rel, shown = order.get("drawing_path"), order.get("drawing_name")
    elif kind == "receipt":
        rel, shown = order.get("receipt_path"), order.get("receipt_name")
    else:
        return jsonify(ok=False, error="附件类型无效"), 400
    if not rel:
        return jsonify(ok=False, error="该订单没有此类附件"), 404
    base = os.path.realpath(config.PO_FILE_DIR)
    path = os.path.realpath(os.path.join(base, rel))
    if not path.startswith(base + os.sep) or not os.path.isfile(path):
        return jsonify(ok=False, error="附件文件不存在"), 404
    # 下载名保留真实扩展名：原始文件名若被改坏/无扩展名，Windows 端会因缺后缀无法直接打开
    shown_name = (shown or "").strip() or os.path.basename(path)
    if not os.path.splitext(shown_name)[1]:
        shown_name += os.path.splitext(path)[1]
    return send_file(path, as_attachment=True, download_name=shown_name)


@app.route("/api/po/stats")
def api_po_stats():
    s, err = po_require()
    if err:
        return err
    return jsonify(ok=True, stats=po_db.stats())


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
