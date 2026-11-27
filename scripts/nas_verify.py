# -*- coding: utf-8 -*-
"""NAS 部署体检脚本（纯 HTTP，不读本地数据库文件，不依赖 /volume1 路径）。

和 scripts/smoke_test.py 的区别：
  smoke_test.py 需要本地 DB 路径、且会**清空数据库**，只能对本机临时实例跑；
  本脚本通过 HTTP 访问已部署的容器，默认**只读**，可以在生产 NAS 上安全执行。

用法：
  # 1) 只读体检（推荐，随时可跑，不改任何数据）
  python3 scripts/nas_verify.py
  python3 scripts/nas_verify.py --base http://192.168.1.10:5088

  # 2) 管理员相关检查需要密码（不传则跳过这部分）
  RECON_PW='你的密码' python3 scripts/nas_verify.py

  # 3) 完整业务流（上传→核对→下载→备份）。**仅允许在空库上跑**，
  #    跑完会调用管理员清空接口还原；库里有真实数据时脚本会直接拒绝执行。
  python3 scripts/nas_verify.py --full

退出码：0 = 全部通过；1 = 有失败项。
"""
import argparse
import io
import os
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("需要 requests：pip install requests")

HEAD = ["供应商 *", "日期 *", "型号 *", "尺寸", "材质", "数量", "单价 *", "合计金额", "备注"]

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  OK   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s  %s" % (name, detail))
    return cond


def make_xlsx(path, rows):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(HEAD)
    for r in rows:
        ws.append(list(r))
    wb.save(path)
    return path


def timed_get(s, url, **kw):
    t0 = time.time()
    r = s.get(url, timeout=kw.pop("timeout", 30), **kw)
    return r, (time.time() - t0) * 1000


def read_only(base, pw):
    s = requests.Session()
    s.trust_env = False

    print("=== 1. 存活与健康 ===")
    r, ms = timed_get(s, base + "/api/status")
    ok = check("/api/status 200 且 ok", r.status_code == 200 and r.json().get("ok"), r.text[:120])
    if ok:
        d = r.json()
        print("     version=%s hist=%s idx=%s db=%.2fMB wal=%.2fMB uptime=%ss 响应=%dms"
              % (d.get("version"), d.get("hist_rows"), d.get("idx_count"),
                 d.get("db_mb") or 0, d.get("wal_mb") or 0, d.get("uptime_sec"), ms))
        check("数据卷可写", d.get("writable") is not False, str(d.get("writable")))
        check("响应时间 < 500ms", ms < 500, "%.0fms" % ms)
        if d.get("version") in (None, ""):
            print("     提示：没有 version 字段，说明跑的是旧镜像（未含 1.1.0 改造）")

    print("\n=== 2. 页面与静态资源 ===")
    for path, min_size in (("/", 3000), ("/static/app.js", 20000), ("/static/style.css", 5000)):
        r, ms = timed_get(s, base + path)
        check("%s 200 且体积正常" % path,
              r.status_code == 200 and len(r.content) >= min_size,
              "code=%s size=%s %dms" % (r.status_code, len(r.content), ms))

    print("\n=== 3. 未登录时的门禁 ===")
    page = s.get(base + "/", timeout=30).text
    check("页面源码不含管理员模块", "管理员可见" not in page, "")
    check("未登录读已导入批次 403", s.get(base + "/api/history/batches", timeout=30).status_code == 403)
    check("未登录读供应商历史 403", s.get(base + "/api/suppliers/history", timeout=30).status_code == 403)
    check("未登录访问诊断接口 403",
          s.get(base + "/api/admin/diagnostics", timeout=30).status_code == 403)

    if not pw:
        print("\n  （未提供 RECON_PW，跳过管理员相关检查）")
        return

    print("\n=== 4. 管理员登录 ===")
    check("旧内置密码被拒",
          s.post(base + "/api/auth/login", json={"password": "13662566518"}, timeout=20).status_code == 403)
    r = s.post(base + "/api/auth/login", json={"password": pw}, timeout=20)
    check("管理员登录成功", r.status_code == 200 and r.json().get("ok"), r.text[:120])
    if not (r.status_code == 200 and r.json().get("ok")):
        return
    check("会话标记为管理员", s.get(base + "/api/auth/status", timeout=20).json().get("is_admin") is True)
    check("登录后页面出现管理员模块", "管理员可见" in s.get(base + "/", timeout=30).text)

    print("\n=== 5. 容器内部诊断 ===")
    r = s.get(base + "/api/admin/diagnostics", timeout=60)
    if not check("诊断接口 200", r.status_code == 200, r.text[:150]):
        return
    d = r.json()
    print("     version=%s python=%s container=%s tz=%s"
          % (d.get("version"), d.get("python"), d.get("container"), d.get("tz")))
    for name, info in (d.get("paths") or {}).items():
        check("路径 %s 存在且可写" % name,
              info.get("exists") and info.get("writable"),
              "%s exists=%s writable=%s" % (info.get("path"), info.get("exists"), info.get("writable")))
    db = d.get("db") or {}
    print("     db: journal=%s page=%s freelist=%s %.2fMB wal=%.2fMB"
          % (db.get("journal_mode"), db.get("page_count"), db.get("freelist"),
             db.get("size_mb") or 0, db.get("wal_mb") or 0))
    check("数据库为 WAL 模式", db.get("journal_mode") == "wal", str(db.get("journal_mode")))
    check("WAL 未异常膨胀（< 50MB）", (db.get("wal_mb") or 0) < 50, "%.2fMB" % (db.get("wal_mb") or 0))
    disk = d.get("disk") or {}
    if disk:
        print("     磁盘: %.1fGB 可用 / %.1fGB 总（已用 %s%%）"
              % (disk.get("free_gb", 0), disk.get("total_gb", 0), disk.get("used_pct")))
        check("剩余空间 > 5GB", (disk.get("free_gb") or 0) > 5, "%.1fGB" % (disk.get("free_gb") or 0))
    cfg = d.get("config") or {}
    check("管理员密码来自环境变量", cfg.get("admin_pwd_from_env") is True, str(cfg.get("admin_pwd_from_env")))
    check("后台维护已启用", cfg.get("maintenance") is True, str(cfg.get("maintenance")))
    backups = d.get("backups") or []
    print("     备份 %d 份，最近：%s" % (len(backups), backups[-1]["name"] if backups else "无"))
    check("存在数据库备份", len(backups) > 0, "备份为空，建议确认 RECON_DAILY_BACKUP")


def full_flow(base, pw):
    """完整业务流；只在空库上执行，避免污染真实历史价。"""
    s = requests.Session()
    s.trust_env = False
    st = s.get(base + "/api/status", timeout=20).json()
    if st.get("hist_rows", 0) != 0:
        sys.exit("拒绝执行 --full：库中已有 %d 条历史数据（真实数据）。\n"
                 "完整业务流会写入测试数据，只能在空库上跑；生产环境请只用默认的只读体检。"
                 % st.get("hist_rows", 0))
    if not pw:
        sys.exit("--full 需要 RECON_PW（管理员密码）")
    r = s.post(base + "/api/auth/login", json={"password": pw}, timeout=20)
    if not r.json().get("ok"):
        sys.exit("管理员登录失败：%s" % r.text[:150])

    import tempfile
    tmp = tempfile.mkdtemp(prefix="recon_full_")
    hist = make_xlsx(os.path.join(tmp, "历史.xlsx"), [
        ("杰盛", "2026-06-05", "X1", "100x200", "304", 10, 12.5, 125.0, ""),
        ("杰盛", "2026-06-05", "X2", "50x50", "201", 20, 5.5, 110.0, ""),
    ])
    pend = make_xlsx(os.path.join(tmp, "待核对.xlsx"), [
        ("杰盛", "2026-08-03", "X1", "100x200", "304", 10, 12.5, 125.0, "一致"),
        ("杰盛", "2026-08-10", "X2", "50x50", "201", 5, 6.5, 32.5, "涨价"),
        ("杰盛", "2026-08-20", "Y9", "10x10", "304", 2, 99.0, 198.0, "无历史"),
    ])

    print("\n=== 6. 完整业务流 ===")
    with open(hist, "rb") as f:
        r = s.post(base + "/api/history/upload",
                   files={"files": (os.path.basename(hist), f, "application/octet-stream")}, timeout=180)
    d = r.json()
    check("历史上传", r.status_code == 200 and d.get("ok"), str(d)[:200])

    with open(pend, "rb") as f:
        r = s.post(base + "/api/reconcile",
                   files={"files": (os.path.basename(pend), f, "application/octet-stream")}, timeout=300)
    d = r.json()
    check("新账单核对", r.status_code == 200 and d.get("ok"), str(d)[:200])
    b = (d.get("batches") or [{}])[0]
    no = b.get("recon_no", "")
    check("四类状态均命中",
          (b.get("matched") or 0) >= 1 and (b.get("above") or 0) >= 1 and (b.get("nohistory") or 0) >= 1,
          str(b)[:200])
    if no:
        r = s.get(base + "/api/download/%s" % no, timeout=300)
        check("Excel 下载", r.status_code == 200 and r.content[:2] == b"PK", "len=%s" % len(r.content))
    r = s.get(base + "/api/history/backup", timeout=300)
    check("备份 ZIP 下载", r.status_code == 200 and r.content[:2] == b"PK", "len=%s" % len(r.content))

    print("\n=== 7. 还原（清空测试数据，清空前自动备份）===")
    r = s.post(base + "/api/history/reset", timeout=180)
    check("清空测试数据", r.status_code == 200 and r.json().get("ok"), r.text[:150])
    st = s.get(base + "/api/status", timeout=20).json()
    check("库已复原为空", st.get("hist_rows") == 0, str(st.get("hist_rows")))


def main():
    ap = argparse.ArgumentParser(description="NAS 部署体检（默认只读）")
    ap.add_argument("--base", default=os.environ.get("RECON_BASE", "http://192.168.1.10:5088"))
    ap.add_argument("--full", action="store_true", help="跑完整业务流（仅限空库）")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    pw = os.environ.get("RECON_PW", "").strip()
    print("目标：%s%s\n" % (base, "（含完整业务流）" if args.full else "（只读体检）"))

    read_only(base, pw)
    if args.full:
        full_flow(base, pw)

    print("\n========================================")
    print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
    print("（日志：NAS 上 /volume3/docker/recon/storage/logs/app.log；"
          "管理员诊断：GET /api/admin/diagnostics）")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
