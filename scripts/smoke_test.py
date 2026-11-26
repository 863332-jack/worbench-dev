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

BASE = "http://127.0.0.1:5088"
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


def main():
    os.makedirs(TMP, exist_ok=True)
    s = requests.Session()

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
    conn = sqlite3.connect(os.path.join(ROOT, "data", "recon.db"))
    row = conn.execute(
        "SELECT quantity, total_amount, bill_date, remark FROM hist_price WHERE model='J55' LIMIT 1"
    ).fetchone()
    conn.close()
    check("quantity=300 total=1200 date=2026-07-03",
          row and row[0] == 300 and row[1] == 1200 and row[2] == "2026-07-03", str(row))

    print("== 10. 管理员清空 + 自动备份 ==")
    r = s.post(BASE + "/api/auth/login", json={"password": "13662566518"}, timeout=10)
    check("管理员登录", r.status_code == 200 and r.json().get("ok"))
    db_size_before = os.path.getsize(os.path.join(ROOT, "data", "recon.db"))
    r = s.post(BASE + "/api/history/reset", timeout=30)
    check("清空成功", r.status_code == 200 and r.json().get("ok"))
    backup = r.json().get("backup", "")
    check("清空前自动备份", bool(backup) and os.path.exists(backup) and os.path.getsize(backup) > 0, backup)
    db_size_after = os.path.getsize(os.path.join(ROOT, "data", "recon.db"))
    check("VACUUM 回收空间", db_size_after < db_size_before,
          "before=%d after=%d" % (db_size_before, db_size_after))
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
    rr = s.post(BASE + "/api/auth/login", json={"password": "13662566518"}, timeout=10)
    check("锁定期间正确密码也拒绝", rr.status_code == 423, "code=%s" % rr.status_code)

    print()
    print("=" * 48)
    print("冒烟测试结果：通过 %d 项 / 失败 %d 项" % (PASS, FAIL))
    print("（登录锁定 10 分钟后自动解除，或重启服务清零）")
    print("=" * 48)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
