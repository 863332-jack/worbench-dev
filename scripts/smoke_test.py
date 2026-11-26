# -*- coding: utf-8 -*-
"""账单核对系统 · 上线前冒烟测试（B-4）。

覆盖：
1. 历史上传（追加、去重、字段入库）
2. 新账单核对（五状态判定）
3. 结果查看（表头一致性）与 Excel 下载
4. 权限：越权清空 403 / 错密码 403 / 登录锁定 423 / 管理员清空 + 自动备份
5. 回归用例：
   - P1-1 模板稀疏数据（双工作表误选）
   - P1-2 CSV 上传拒绝（承诺与实现一致）
   - 严格门禁（错误数据整批拒绝、零写入）

用法：服务启动后执行  python3 scripts/smoke_test.py
依赖：requests、openpyxl。退出码 0=全部通过。
"""
import os
import sys
import glob
import time

import requests
from openpyxl import Workbook

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:5088")   # 可指向临时实例
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(ROOT, "scripts", ".smoke_tmp")
HEADERS_STAR = ["供应商 *", "日期 *", "型号 *", "尺寸", "材质", "数量", "单价 *", "合计金额", "备注"]

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % name)
    else:
        FAIL += 1
        print("  ✗ %s  %s" % (name, detail))


def make_xlsx(path, rows, sheet="Sheet1", extra_sheets=None):
    """生成标准 9 列账单文件；extra_sheets: {表名: [[行], ...]} 用于双表场景。"""
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(HEADERS_STAR)
    for r in rows:
        ws.append(list(r))
    for nm, rs in (extra_sheets or {}).items():
        w2 = wb.create_sheet(nm)
        for r in rs:
            w2.append(list(r))
    wb.save(path)
    return path


def guard_live_data():
    """安全闸门：冒烟测试会导入并**清空**历史库、删除测试订单。

    2026-11-26 起系统已在局域网实际使用（真实历史/核对数据可能已存在），
    因此默认拒绝在有任何业务数据时运行；确需运行时显式设置 SMOKE_FORCE=1。
    """
    import sqlite3
    db_path = os.environ.get("SMOKE_DB_PATH") or os.path.join(ROOT, "data", "recon.db")
    if not os.path.exists(db_path):
        return
    conn = sqlite3.connect(db_path)
    busy = {}
    for t in ("hist_price", "recon_batch", "recon_result"):
        try:
            busy[t] = conn.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]
        except sqlite3.Error:
            busy[t] = 0
    conn.close()
    if any(v for v in busy.values()) and os.environ.get("SMOKE_FORCE") != "1":
        print("=" * 60)
        print("已中止：数据库中存在业务数据 %s" % busy)
        print("冒烟测试会导入并清空历史库，为避免破坏真实数据默认不执行。")
        print("确认可以清空时：先备份 data/recon.db，再用 SMOKE_FORCE=1 重新运行。")
        print("=" * 60)
        raise SystemExit(2)


def main():
    guard_live_data()
    os.makedirs(TMP, exist_ok=True)
    s = requests.Session()
    # 直连库的路径同样参数化，临时实例下断言才不会落到实盘库上
    DB_PATH = os.environ.get("SMOKE_DB_PATH") or os.path.join(ROOT, "data", "recon.db")

    print("== 1. 系统状态 ==")
    r = s.get(BASE + "/api/status", timeout=10).json()
    check("status 接口可用", r.get("ok") is True)

    print("== 2. 历史上传（追加 + 去重 + 字段入库） ==")
    hist_rows = [
        ["杰盛", "2026-07-03", "J55", "15*8", "铜", 300, 4, 1200, ""],
        ["杰盛", "2026-07-03", "J83", "20*10", "铜", 500, 3.5, 1750, ""],
        ["云锦", "2026-08-05", "NP-50", "", "塑料", 100, 6, 600, ""],
        ["杰盛", "2026-09-10", "J99", "", "铁", 50, 2.5, 125, ""],
        ["新新精密", "2026-08-20", "M12", "D12*40", "铝", 80, 5, 400, ""],
        ["新新精密", "2026-09-20", "M12", "D12*40", "铝", 80, 7, 560, ""],  # 同型号两档价
    ]
    f_hist = make_xlsx(os.path.join(TMP, "hist.xlsx"), hist_rows)
    r = s.post(BASE + "/api/history/upload", files={"files": open(f_hist, "rb")}, timeout=30).json()
    check("首次导入 ok", r.get("ok") is True, str(r)[:200])
    check("新增 6 条", r.get("added") == 6, "added=%s" % r.get("added"))
    check("累计 6 条 / 基准 5 个", r.get("hist_rows") == 6 and r.get("idx_count") == 5,
          "hist=%s idx=%s" % (r.get("hist_rows"), r.get("idx_count")))

    r2 = s.post(BASE + "/api/history/upload", files={"files": open(f_hist, "rb")}, timeout=30).json()
    check("重复文件全跳过 added=0", r2.get("ok") and r2.get("added") == 0,
          "added=%s" % r2.get("added"))

    print("== 3. 新账单核对（五状态判定） ==")
    bill_rows = [
        ["杰盛", "2026-10-03", "J55", "15*8", "铜", 200, 4, 800, ""],      # 匹配最低
        ["杰盛", "2026-10-03", "J55", "15*8", "铜", 100, 5, 500, ""],      # 高于最低
        ["杰盛", "2026-10-03", "J83", "20*10", "铜", 300, 3.5, 1050, ""],  # 匹配最低
        ["云锦", "2026-10-05", "NP-50", "", "塑料", 60, 6, 360, ""],       # 匹配最低
        ["杰盛", "2026-10-03", "XX-40", "", "铁", 10, 9, 90, ""],          # 无历史
        ["新新精密", "2026-10-20", "M12", "D12*40", "铝", 40, 5, 200, ""], # 多档价→复核
    ]
    f_bill = make_xlsx(os.path.join(TMP, "bill.xlsx"), bill_rows)
    r = s.post(BASE + "/api/reconcile", files={"files": open(f_bill, "rb")}, timeout=30).json()
    check("核对 ok", r.get("ok") is True, str(r)[:200])
    c = r.get("counts", {})
    check("状态判定：匹配3/高于1/复核1/无历史1",
          c.get("matched") == 3 and c.get("above") == 1 and c.get("review") == 1
          and c.get("nohistory") == 1, str(c))
    recon_no = r["batches"][0]["recon_no"]

    print("== 4. 结果查看（表头一致）与下载 ==")
    r = s.get(BASE + "/api/results/" + recon_no, timeout=10).json()
    check("结果 6 条", r.get("ok") and len(r.get("results", [])) == 6)
    check("表头与上传完全一致（9 列含*）", r.get("headers") == HEADERS_STAR,
          "headers=%s" % r.get("headers"))
    check("明细行原始值按表头长度输出", all(len(x.get("raw_values", [])) == 9 for x in r["results"]))
    resp = s.get(BASE + "/api/download/" + recon_no, timeout=30)
    check("Excel 下载 200 且为 xlsx", resp.status_code == 200 and resp.content[:2] == b"PK")

    print("== 5. 权限控制 ==")
    r = requests.post(BASE + "/api/history/reset", timeout=10)
    check("未登录清空被拒 403", r.status_code == 403)
    r = s.post(BASE + "/api/auth/login", json={"password": "wrong"}, timeout=10)
    check("错误密码 403", r.status_code == 403)

    print("== 6. 回归：P1-1 模板稀疏数据（双工作表） ==")
    note_rows = [["字段说明第 %d 行" % i, "内容内容内容内容内容"] for i in range(19)]
    f_sparse = make_xlsx(os.path.join(TMP, "sparse.xlsx"),
                         [["杰盛", "2026-07-03", "SP-1", "", "铜", 10, 4, 40, ""],
                          ["云锦", "2026-08-05", "SP-2", "", "塑料", 20, 6, 120, ""],
                          ["杰盛", "2026-07-03", "SP-3", "", "铁", 30, 2.5, 75, ""]],
                         sheet="账单数据", extra_sheets={"填写说明": note_rows})
    r = s.post(BASE + "/api/history/upload", files={"files": open(f_sparse, "rb")}, timeout=30).json()
    check("稀疏数据上传成功 3 条", r.get("ok") and r.get("added") == 3,
          str(r)[:300])
    check("选中「账单数据」工作表", any(rep.get("sheet") == "账单数据" for rep in r.get("reports", [])))

    print("== 7. 回归：P1-2 CSV 拒绝（承诺一致） ==")
    f_csv = os.path.join(TMP, "t.csv")
    with open(f_csv, "w") as f:
        f.write("供应商,日期,型号,尺寸,材质,数量,单价,合计金额,备注\n杰盛,2026-07-03,CV-1,,铜,1,4,4,\n")
    r = s.post(BASE + "/api/history/upload", files={"files": open(f_csv, "rb")}, timeout=10).json()
    check("CSV 被明确拒绝", r.get("ok") is False and "文件格式" in str(r.get("errors", [{}])[0].get("field", "")),
          str(r)[:200])

    print("== 8. 回归：严格门禁（错误数据整批拒绝、零写入） ==")
    before = s.get(BASE + "/api/status", timeout=10).json()["hist_rows"]
    bad_rows = [
        ["杰盛", "2026-07-03", "B-1", "", "铜", 1, 4, 4, ""],
        ["", "2026-07-03", "B-2", "", "铜", 1, 4, 4, ""],          # 缺供应商
        ["杰盛", "7月1", "B-3", "", "铜", 1, 4, 4, ""],            # 日期格式错误
        ["杰盛", "2026-07-03", "B-4", "", "铜", 1, "待核价", 4, ""],  # 待核价
    ]
    f_bad = make_xlsx(os.path.join(TMP, "bad.xlsx"), bad_rows)
    r = s.post(BASE + "/api/history/upload", files={"files": open(f_bad, "rb")}, timeout=10).json()
    check("错误数据整批拒绝", r.get("ok") is False and len(r.get("errors", [])) == 3,
          "errors=%s" % len(r.get("errors", [])))
    after = s.get(BASE + "/api/status", timeout=10).json()["hist_rows"]
    check("库零写入", after == before, "before=%s after=%s" % (before, after))

    print("== 9. 数量/合计金额入库（B-1） ==")
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT quantity, total_amount, bill_date, remark FROM hist_price WHERE model='J55' LIMIT 1"
    ).fetchone()
    conn.close()
    check("quantity=300 total=1200 date=2026-07-03",
          row and row[0] == 300 and row[1] == 1200 and row[2] == "2026-07-03", str(row))

    print("== 10. 管理员清空 + 自动备份 ==")
    r = s.post(BASE + "/api/auth/login", json={"password": "13662566518"}, timeout=10)
    check("管理员登录", r.status_code == 200 and r.json().get("ok"))
    db_size_before = os.path.getsize(DB_PATH)
    r = s.post(BASE + "/api/history/reset", timeout=30)
    check("清空成功", r.status_code == 200 and r.json().get("ok"))
    backup = r.json().get("backup", "")
    check("清空前自动备份", bool(backup) and os.path.exists(backup) and os.path.getsize(backup) > 0, backup)
    db_size_after = os.path.getsize(DB_PATH)
    conn2 = sqlite3.connect(DB_PATH)
    free = conn2.execute("PRAGMA freelist_count").fetchone()[0]
    conn2.close()
    # 断言 VACUUM 真实效果：空闲页归零且文件未增大（小数据量下清空前后页数相同属正常）
    check("VACUUM 回收空间（空闲页归零）", free == 0 and db_size_after <= db_size_before,
          "before=%d after=%d free=%d" % (db_size_before, db_size_after, free))
    r = s.get(BASE + "/api/status", timeout=10).json()
    check("清空后库为空", r["hist_rows"] == 0 and r["idx_count"] == 0)

    print("== 11. 登录失败锁定（A-7） ==")
    s.post(BASE + "/api/auth/logout", timeout=10)
    locked = None
    for i in range(5):
        rr = s.post(BASE + "/api/auth/login", json={"password": "bad%d" % i}, timeout=10)
        if rr.status_code == 423:
            locked = i + 1
            break
    check("连续失败触发锁定 423", locked is not None, "第 %s 次" % locked)
    rr = s.post(BASE + "/api/auth/login", json={"password": "bad_after_lock"}, timeout=10)
    check("锁定期内错误密码继续 423", rr.status_code == 423, "code=%s" % rr.status_code)
    rr = s.post(BASE + "/api/auth/login", json={"password": "13662566518"}, timeout=10)
    check("正确密码可即时登录（管理员不自锁）",
          rr.status_code == 200 and rr.json().get("ok"), "code=%s" % rr.status_code)
    s.post(BASE + "/api/auth/logout", timeout=10)

    print("== 12. 供应商管理（可扩展：新增后下拉/校验名单同步生效） ==")
    r = s.get(BASE + "/api/suppliers", timeout=10).json()
    names = [x["name"] for x in r.get("suppliers", [])]
    check("供应商列表可用且含内置三家",
          r.get("ok") is True and {"杰盛", "新新精密", "云锦"} <= set(names), str(names))

    r = s.post(BASE + "/api/suppliers", json={"name": "   "}, timeout=10)
    check("空名称被拒 400", r.status_code == 400 and r.json().get("ok") is False, r.text[:120])

    r = s.post(BASE + "/api/suppliers", json={"name": "云锦 "}, timeout=10)
    check("重复供应商被拒（空格/全半角视为同一家）", r.status_code == 400, r.text[:160])

    r = s.post(BASE + "/api/suppliers",
               json={"name": " 恒达 五金 ", "alias": "恒达,HD", "remark": "冒烟测试"}, timeout=10).json()
    check("新增供应商 ok 且名称已归一化",
          r.get("ok") is True and r.get("supplier", {}).get("name") == "恒达 五金", str(r)[:160])
    check("新增后返回最新列表", "恒达 五金" in [x["name"] for x in r.get("suppliers", [])],
          str(r.get("suppliers"))[:200])

    # 新增的供应商必须立刻进入上传校验名单（不再写死三家）
    f_new = make_xlsx(os.path.join(TMP, "new_sup.xlsx"),
                      [["恒达 五金", "2026-11-01", "HD-1", "10*10", "铜", 10, 3, 30, ""]])
    r = s.post(BASE + "/api/history/upload", files={"files": open(f_new, "rb")}, timeout=30).json()
    check("新供应商账单可通过校验并入库", r.get("ok") is True and r.get("added") == 1, str(r)[:200])

    f_bad = make_xlsx(os.path.join(TMP, "bad_sup.xlsx"),
                      [["尚未建档的供应商", "2026-11-01", "X-1", "", "铜", 1, 3, 3, ""]])
    r = s.post(BASE + "/api/history/upload", files={"files": open(f_bad, "rb")}, timeout=30).json()
    check("未建档供应商仍被拒绝（名单未被放宽）",
          r.get("ok") is False and any(e.get("field") == "供应商" for e in (r.get("errors") or [])),
          str(r)[:200])

    sid = [x["id"] for x in s.get(BASE + "/api/suppliers", timeout=10).json()["suppliers"]
           if x["name"] == "恒达 五金"][0]
    r = requests.post(BASE + "/api/suppliers/%d/delete" % sid, timeout=10)
    check("未登录删除被拒 403", r.status_code == 403, r.text[:120])

    s.post(BASE + "/api/auth/login", json={"password": "13662566518"}, timeout=10)
    r = s.post(BASE + "/api/suppliers/%d/delete" % sid, timeout=10).json()
    check("已被历史引用的供应商拒绝删除",
          r.get("ok") is False and "已被使用" in (r.get("error") or ""), str(r)[:160])

    s.post(BASE + "/api/suppliers", json={"name": "临时未使用供应商"}, timeout=10)
    sid2 = [x["id"] for x in s.get(BASE + "/api/suppliers", timeout=10).json()["suppliers"]
            if x["name"] == "临时未使用供应商"][0]
    r = s.post(BASE + "/api/suppliers/%d/delete" % sid2, timeout=10).json()
    check("无引用供应商可删除",
          r.get("ok") is True and "临时未使用供应商" not in [x["name"] for x in r.get("suppliers", [])],
          str(r)[:160])

    print("== 13. 账单备份导出（筛选 → 打包 → 可回灌） ==")
    import io
    import json as _json
    import zipfile
    bk_rows = [["杰盛", "2027-01-02", "BK-1", "8*8", "铜", 10, 4, 40, ""],
               ["杰盛", "2027-01-03", "BK-2", "8*8", "铜", 5, 6, 30, ""],
               ["云锦", "2027-02-05", "BK-3", "", "塑料", 20, 3, 60, ""]]
    f_bk = make_xlsx(os.path.join(TMP, "bk_hist.xlsx"), bk_rows)
    r = s.post(BASE + "/api/history/upload", files={"files": open(f_bk, "rb")}, timeout=30).json()
    check("备份用历史数据导入 ok", r.get("ok") is True and r.get("added") == 3, str(r)[:160])

    r = s.get(BASE + "/api/history/backup/preview?kinds=history&month_from=2027-01&month_to=2027-01",
              timeout=20).json()
    check("备份预览：仅 1 月 → 2 条 / 1 个文件",
          r.get("ok") and r["totals"]["rows"] == 2 and r["totals"]["files"] == 1,
          str(r.get("totals")))
    r = s.get(BASE + "/api/history/backup/preview?kinds=history&month_from=2027-02&month_to=2027-01", timeout=20)
    check("备份：起止月份倒置被拒 400", r.status_code == 400, str(r.status_code))
    r = s.get(BASE + "/api/history/backup/preview?kinds=history&month_from=2027-1", timeout=20)
    check("备份：月份格式错误被拒 400", r.status_code == 400, str(r.status_code))

    resp = s.get(BASE + "/api/history/backup?kinds=history&month_from=2027-01&month_to=2027-02", timeout=60)
    zpath = os.path.join(TMP, "bk.zip")
    open(zpath, "wb").write(resp.content)
    check("备份打包 200 且为 ZIP", resp.status_code == 200 and resp.content[:2] == b"PK", str(resp.status_code))
    zf = zipfile.ZipFile(zpath)
    names = zf.namelist()
    check("备份包含清单(CSV/JSON)与恢复说明",
          {"备份清单.csv", "备份清单.json", "恢复说明.txt"} <= set(names), str(names))
    man = _json.loads(zf.read("备份清单.json").decode("utf-8"))
    check("备份清单行数与筛选一致", man["totals"]["rows"] == 3, str(man.get("totals")))
    check("备份清单含 SHA256 校验值", all(len(f["sha256"]) == 16 for f in man["files"]), "")
    xs = [n for n in names if n.endswith(".xlsx")]
    check("分表命名可归档（分表以「历史账单_供应商_期间」命名）",
          len(xs) >= 2 and all(n.startswith("历史账单_") and n.count("_") >= 2 for n in xs), str(xs))
    zp = os.path.join(TMP, "bk_unzip")
    os.makedirs(zp, exist_ok=True)
    zf.extractall(zp)
    before = s.get(BASE + "/api/status", timeout=10).json()["hist_rows"]
    rr = s.post(BASE + "/api/history/upload",
                files=[("files", open(os.path.join(zp, n), "rb")) for n in xs], timeout=60).json()
    after = s.get(BASE + "/api/status", timeout=10).json()["hist_rows"]
    check("备份分表可回灌（格式合法）且不产生重复",
          rr.get("ok") is True and after == before,
          "ok=%s before=%s after=%s" % (rr.get("ok"), before, after))

    print("== 14. 智能对账：差异量化与汇总 ==")
    b2 = [["杰盛", "2027-03-10", "BK-1", "8*8", "铜", 10, 5, 50, ""],      # 单价 +1 → 金额 +10
          ["杰盛", "2027-03-10", "BK-2", "8*8", "铜", 5, 5, 25, ""],       # 单价 -1 → 金额 -5
          ["云锦", "2027-03-10", "BK-3", "", "塑料", 20, 3, 60, ""]]       # 持平
    f_b2 = make_xlsx(os.path.join(TMP, "bk_bill.xlsx"), b2)
    r = s.post(BASE + "/api/reconcile", files={"files": open(f_b2, "rb")}, timeout=30).json()
    c = r.get("counts", {})
    check("核对返回差异金额：超付 10 / 少付 -5",
          abs(c.get("overpay", 0) - 10) < 0.01 and abs(c.get("underpay", 0) + 5) < 0.01, str(c))
    check("差异笔数 = 2", c.get("diff") == 2, str(c.get("diff")))
    no2 = r["batches"][0]["recon_no"]
    res = s.get(BASE + "/api/results/" + no2, timeout=20).json()
    amt = (res.get("summary") or {}).get("amount", {})
    check("统计口径：本单 135 / 应有 130 / 净差异 5",
          abs(amt.get("bill_total", 0) - 135) < 0.01 and abs(amt.get("should_total", 0) - 130) < 0.01
          and abs(amt.get("net_gap", 0) - 5) < 0.01, str(amt))
    codes = {d["code"] for d in (res.get("summary") or {}).get("by_diff", [])}
    check("差异类型含 金额偏高 / 金额偏低", {"amt_high", "amt_low"} <= codes, str(codes))
    row = [x for x in res["results"] if x["model"] == "BK-1"][0]
    check("单行差异：单价 +1 / 差异率 25%（1÷4）/ 金额 +10",
          abs(row["price_gap"] - 1) < 1e-6 and abs(row["gap_rate"] - 0.25) < 1e-6
          and abs(row["amount_gap"] - 10) < 0.01, str(row)[:180])
    rf = s.get(BASE + "/api/results/%s?diff=amt_high" % no2, timeout=20).json()
    check("按差异类型筛选生效（只返回 1 条）", len(rf.get("results", [])) == 1,
          str(len(rf.get("results", []))))
    resp = s.get(BASE + "/api/download/" + no2, timeout=30)
    open(os.path.join(TMP, "bk_result.xlsx"), "wb").write(resp.content)
    from openpyxl import load_workbook
    wb2 = load_workbook(os.path.join(TMP, "bk_result.xlsx"))
    check("结果 Excel 含「差异汇总」工作表", "差异汇总" in wb2.sheetnames, str(wb2.sheetnames))

    print("== 15. 发货明细完整入库（v2 去重：时间与内容全部参与） ==")
    det_rows = [["云锦", "2027-05-05", "DET-1", "", "铜", 10, 5, 50, ""],
                ["云锦", "2027-05-05", "DET-1", "", "铜", 20, 5, 100, ""],  # 同日同价、数量不同
                ["云锦", "2027-05-05", "DET-1", "", "铜", 10, 5, 50, ""],   # 与首行完全相同（同日两笔）
                ["云锦", "2027-05-06", "DET-1", "", "铜", 10, 5, 50, ""],   # 仅日期不同
                ["云锦", "2027-05-05", "DET-2", "", "铜", 10, 5, 50, ""]]   # 仅型号不同
    f_det = make_xlsx(os.path.join(TMP, "det.xlsx"), det_rows)
    r = s.post(BASE + "/api/history/upload", files={"files": open(f_det, "rb")}, timeout=30).json()
    check("同月同价、日期/数量不同的明细全部入库（added=5）",
          r.get("ok") is True and r.get("added") == 5, str(r)[:220])
    r2 = s.post(BASE + "/api/history/upload", files={"files": open(f_det, "rb")}, timeout=30).json()
    check("重复上传同一文件仍不产生重复（added=0）",
          r2.get("ok") is True and r2.get("added") == 0, str(r2)[:220])
    conn3 = sqlite3.connect(os.environ.get("SMOKE_DB_PATH") or os.path.join(ROOT, "data", "recon.db"))
    got = conn3.execute("SELECT bill_date, quantity, total_amount FROM hist_price "
                        "WHERE model='DET-1' ORDER BY bill_date, quantity").fetchall()
    conn3.close()
    check("库内明细可逐条还原（05-05 三条 + 05-06 一条）",
          len(got) == 4 and [g[1] for g in got] == [10.0, 10.0, 20.0, 10.0], str(got))
    check("数量/合计金额未被合并丢失", all(g[2] == g[1] * 5 for g in got), str(got))

    print("== 16. 供应商历史有效数据台账（按供应商×期间） ==")
    r = s.get(BASE + "/api/suppliers/history", timeout=20).json()
    check("台账接口可用且有期间记录", r.get("ok") is True and len(r.get("rows", [])) >= 3,
          str(r)[:160])
    check("快照含时间/来源/状态/关键指标",
          all(k in r["rows"][0] for k in ("period", "rows_total", "rows_valid", "models",
                                          "min_price", "avg_price", "amount_total", "files",
                                          "status", "updated_at")), str(r["rows"][0])[:200])
    check("有效期间只统计有效明细（合计>0）",
          r["summary"]["rows_valid"] > 0 and r["summary"]["periods"] == len(
              [x for x in r["rows"] if x["rows_valid"] > 0]), str(r["summary"]))

    r2 = s.get(BASE + "/api/suppliers/history?supplier=%E6%9D%B0%E7%9B%9B"
                      "&month_from=2027-01&month_to=2027-01", timeout=20).json()
    check("按供应商 + 月份范围查询生效",
          len(r2["rows"]) == 1 and r2["rows"][0]["supplier"] == "杰盛"
          and r2["rows"][0]["period"] == "2027-01", str(r2["rows"])[:200])

    sh_add = make_xlsx(os.path.join(TMP, "sh_add.xlsx"),
                       [["杰盛", "2027-01-20", "SH-NEW", "", "铜", 3, 9, 27, ""]])
    s.post(BASE + "/api/history/upload", files={"files": open(sh_add, "rb")}, timeout=30)
    r3 = s.get(BASE + "/api/suppliers/history?supplier=%E6%9D%B0%E7%9B%9B"
                      "&month_from=2027-01&month_to=2027-01", timeout=20).json()
    row = r3["rows"][0]
    check("新增数据后期间状态转为「变更」并给出差异",
          row["status"] == "changed" and "+1" in (row["change_note"] or ""), str(row)[:240])
    ev = s.get(BASE + "/api/suppliers/history?supplier=%E6%9D%B0%E7%9B%9B", timeout=20).json()["events"]
    check("状态变化已记入事件流",
          any(e["event"] in ("new", "changed") for e in ev), str(ev[:2])[:200])

    ev_before = len(ev)
    s.post(BASE + "/api/history/upload", files={"files": open(sh_add, "rb")}, timeout=30)
    ev_after = len(s.get(BASE + "/api/suppliers/history?supplier=%E6%9D%B0%E7%9B%9B",
                         timeout=20).json()["events"])
    check("重复导入不重复写台账/事件", ev_after == ev_before, "%d → %d" % (ev_before, ev_after))

    s.post(BASE + "/api/auth/login", json={"password": "13662566518"}, timeout=10)
    s.post(BASE + "/api/history/reset", timeout=30)
    r4 = s.get(BASE + "/api/suppliers/history", timeout=20).json()
    check("清空后各期间转「失效」且不计入有效记录",
          r4["summary"]["periods"] == 0 and r4["summary"]["rows_valid"] == 0
          and all(x["rows_valid"] == 0 for x in r4["rows"]), str(r4["summary"]))

    print()
    print("=" * 48)
    print("冒烟测试结果：通过 %d 项 / 失败 %d 项" % (PASS, FAIL))
    print("（登录锁定 10 分钟后自动解除，或重启服务清零）")
    print("=" * 48)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
