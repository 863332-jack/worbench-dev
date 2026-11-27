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

    print("== 13. 账单备份导出（仅管理员 · 按供应商 · 全部期间） ==")
    import json as _json
    import sqlite3
    import zipfile
    bk_rows = [["杰盛", "2027-01-02", "BK-1", "8*8", "铜", 10, 4, 40, ""],
               ["杰盛", "2027-01-03", "BK-2", "8*8", "铜", 5, 6, 30, ""],
               ["云锦", "2027-02-05", "BK-3", "", "塑料", 20, 3, 60, ""]]
    f_bk = make_xlsx(os.path.join(TMP, "bk_hist.xlsx"), bk_rows)
    r = s.post(BASE + "/api/history/upload", files={"files": open(f_bk, "rb")}, timeout=30).json()
    check("备份用历史数据导入 ok", r.get("ok") is True and r.get("added") == 3, str(r)[:160])

    # --- 权限：非管理员一律 403，且提示明确 ---
    s.post(BASE + "/api/auth/logout", timeout=10)
    r = s.get(BASE + "/api/history/backup/preview?kinds=history", timeout=20)
    check("非管理员：预览导出被拒 403 且提示明确",
          r.status_code == 403 and "仅管理员" in (r.json().get("error") or ""),
          "%s %s" % (r.status_code, r.text[:120]))
    r = s.get(BASE + "/api/history/backup?kinds=history", timeout=20)
    check("非管理员：打包下载被拒 403 且提示明确",
          r.status_code == 403 and "仅管理员" in (r.json().get("error") or ""),
          "%s %s" % (r.status_code, r.text[:120]))
    page = s.get(BASE + "/", timeout=20).text
    check("非管理员：页面源码中根本没有导出入口（服务端不渲染）",
          "账单备份导出" not in page and 'id="bkCard"' not in page
          and 'id="btnBkDownload"' not in page, "len=%d" % len(page))

    # --- 新增门禁：已导入批次 / 供应商历史有效数据（同样仅管理员） ---
    r = s.get(BASE + "/api/history/batches", timeout=20)
    check("非管理员：已导入批次接口被拒 403 且提示明确",
          r.status_code == 403 and "仅管理员" in (r.json().get("error") or ""),
          "%s %s" % (r.status_code, r.text[:120]))
    r = s.get(BASE + "/api/suppliers/history", timeout=20)
    check("非管理员：供应商历史台账接口被拒 403 且提示明确",
          r.status_code == 403 and "仅管理员" in (r.json().get("error") or ""),
          "%s %s" % (r.status_code, r.text[:120]))
    check("非管理员：页面源码中没有「已导入批次」与「供应商历史有效数据」模块",
          'id="foldBatches"' not in page and 'id="histBatches"' not in page
          and 'id="foldSupHist"' not in page and 'id="shTable"' not in page,
          "len=%d" % len(page))

    # --- 16c. 型号历史查询：所有用户可用、逐条明细、只读 ---
    # 与上一个功能（管理员台账里的型号筛选）不同：这块在主页面始终渲染，不校验管理员会话。
    check("非管理员：主页面渲染出型号查询框（所有用户可用）",
          'id="msCard"' in page and 'id="msInput"' in page, "len=%d" % len(page))
    n_before = s.get(BASE + "/api/status", timeout=20).json()["hist_rows"]
    rq = s.get(BASE + "/api/history/search?model=BK-", timeout=20)
    d = rq.json()
    check("非管理员：型号历史查询接口可用（不要求管理员）",
          rq.status_code == 200 and d.get("ok") is True and d.get("total", 0) >= 1,
          "%s total=%s" % (rq.status_code, d.get("total")))
    first = (d.get("rows") or [{}])[0]
    check("结果含型号/记录标识/上传时间/状态等已有字段",
          all(k in first for k in ("model", "supplier", "period", "unit_price", "quantity",
                                   "batch_no", "record_id", "source_file", "source_row",
                                   "uploaded_at", "status")), str(first)[:240])
    check("返回的是逐条明细（总数=明细条数，不是聚合行）",
          len(d["rows"]) == d["total"] and all(x.get("model") for x in d["rows"]),
          "%d 行 / total %d" % (len(d["rows"]), d["total"]))
    check("状态与有效口径一致（有效行必有正单价）",
          all(x["status"] == "有效" for x in d["rows"] if (x.get("unit_price") or 0) > 0
              and not x.get("exclude_reason")), str([x["status"] for x in d["rows"]])[:160])
    rq_low = s.get(BASE + "/api/history/search?model=bk-", timeout=20).json()
    check("型号查询：包含匹配且忽略大小写", rq_low["total"] == d["total"],
          "%s vs %s" % (rq_low["total"], d["total"]))
    rq_none = s.get(BASE + "/api/history/search?model=%E4%B8%8D%E5%AD%98%E5%9C%A8ZZZ",
                    timeout=20).json()
    check("型号查询：无匹配返回空结果且不报错",
          rq_none.get("ok") is True and rq_none["total"] == 0 and rq_none["rows"] == [],
          str(rq_none.get("total")))
    rq_empty = s.get(BASE + "/api/history/search?model=", timeout=20).json()
    check("型号查询：空关键字不返回全量（不泄露整库）",
          rq_empty.get("ok") is True and rq_empty["total"] == 0, str(rq_empty.get("total")))
    rq_wc = s.get(BASE + "/api/history/search?model=%25", timeout=20).json()
    check("型号查询：通配符被转义（% 不当通配符用）", rq_wc["total"] == 0, str(rq_wc["total"]))
    rq_long = s.get(BASE + "/api/history/search?model=" + "A" * 61, timeout=20)
    check("型号查询：超长关键字被拒 400", rq_long.status_code == 400, str(rq_long.status_code))
    rq_p = s.get(BASE + "/api/history/search?model=BK-&limit=2", timeout=20).json()
    check("型号查询：分页 limit 生效并标记截断",
          len(rq_p["rows"]) == 2 and rq_p["truncated"] is True and rq_p["total"] == d["total"],
          "%d 行 truncated=%s" % (len(rq_p["rows"]), rq_p.get("truncated")))
    rq_p2 = s.get(BASE + "/api/history/search?model=BK-&limit=2&offset=2", timeout=20).json()
    check("型号查询：offset 翻页不重复",
          not ({x["id"] for x in rq_p["rows"]} & {x["id"] for x in rq_p2["rows"]}),
          str([x["id"] for x in rq_p["rows"]]) + " / " + str([x["id"] for x in rq_p2["rows"]]))
    n_after = s.get(BASE + "/api/status", timeout=20).json()["hist_rows"]
    check("型号历史查询全程只读（历史库行数不变）", n_before == n_after,
          "%s → %s" % (n_before, n_after))

    # --- 管理员登录后可用 ---
    s.post(BASE + "/api/auth/login", json={"password": "13662566518"}, timeout=10)
    page = s.get(BASE + "/", timeout=20).text
    check("管理员：页面才渲染出导出入口",
          "账单备份导出" in page and 'id="bkCard"' in page and 'id="btnBkDownload"' in page,
          "len=%d" % len(page))
    r = s.get(BASE + "/api/history/batches", timeout=20)
    check("管理员：已导入批次接口恢复可用",
          r.status_code == 200 and r.json().get("ok") is True, str(r.status_code))
    r = s.get(BASE + "/api/suppliers/history", timeout=20)
    check("管理员：供应商历史台账接口恢复可用",
          r.status_code == 200 and r.json().get("ok") is True, str(r.status_code))
    check("管理员：页面才渲染出「已导入批次」与「供应商历史有效数据」模块",
          'id="foldBatches"' in page and 'id="histBatches"' in page
          and 'id="foldSupHist"' in page and 'id="shTable"' in page,
          "len=%d" % len(page))

    # --- 按月份导出已取消 ---
    r = s.get(BASE + "/api/history/backup/preview?kinds=history&month_from=2027-01", timeout=20)
    check("按月导出参数被拒 400（已取消按月导出）",
          r.status_code == 400 and "按月" in (r.json().get("error") or ""),
          "%s %s" % (r.status_code, r.text[:120]))
    r = s.get(BASE + "/api/history/backup/preview?kinds=history&month_from=2027-02&month_to=2027-01",
              timeout=20)
    check("按月导出仍被拒（不再做月份校验逻辑）", r.status_code == 400, str(r.status_code))

    # --- 预览：按供应商分类、覆盖全部期间 ---
    _ck = sqlite3.connect(os.environ.get("SMOKE_DB_PATH") or os.path.join(ROOT, "data", "recon.db"))
    _valid = "COALESCE(exclude_reason,'')=''"
    n_rows = _ck.execute("SELECT COUNT(*) FROM hist_price WHERE " + _valid).fetchone()[0]
    n_sup = _ck.execute("SELECT COUNT(DISTINCT supplier) FROM hist_price WHERE " + _valid).fetchone()[0]
    n_pairs = _ck.execute("SELECT COUNT(*) FROM (SELECT DISTINCT supplier, period FROM hist_price"
                          " WHERE " + _valid + ")").fetchone()[0]
    _ck.close()
    r = s.get(BASE + "/api/history/backup/preview?kinds=history", timeout=20).json()
    check("预览：行数=库内全部有效历史记录（不再受月份限制）",
          r.get("ok") and r["totals"]["rows"] == n_rows, "%s vs %s" % (r.get("totals"), n_rows))
    check("预览：文件数=供应商数（一家一个分表）",
          r["totals"]["files"] == n_sup, "%s vs %s" % (r["totals"]["files"], n_sup))
    _multi = n_pairs > n_sup      # 库里确实存在跨月供应商时，才能反证「未按月拆分」
    check("未按月拆分：文件数=供应商数，而非「供应商×期间」数",
          r["totals"]["files"] == n_sup and (not _multi or n_pairs > r["totals"]["files"]),
          "files=%s suppliers=%s pairs=%s" % (r["totals"]["files"], n_sup, n_pairs))
    check("预览：分表命名为「类型_供应商_全部记录」",
          all(f["name"].endswith("_全部记录.xlsx") for f in r["files"]),
          str([f["name"] for f in r["files"]])[:200])
    check("预览：返回导出范围说明", "全部期间" in (r.get("scope") or ""), str(r.get("scope")))
    _sups = sorted({f["supplier"] for f in r["files"]})
    _want = _sups[0] if len(_sups) == 1 else "%d家供应商" % len(_sups)
    check("ZIP 名称与实际导出的供应商一致（不写未命中的家数）",
          _want in r["zip_name"], "%s / %s" % (_want, r["zip_name"]))

    # --- 只导出指定供应商 ---
    r = s.get(BASE + "/api/history/backup/preview?kinds=history&suppliers=%E6%9D%B0%E7%9B%9B",
              timeout=20).json()
    check("按供应商筛选生效（只出该供应商一个分表）",
          r["totals"]["files"] == 1 and r["files"][0]["supplier"] == "杰盛", str(r["totals"]))

    # --- 打包下载 ---
    resp = s.get(BASE + "/api/history/backup?kinds=history", timeout=60)
    zpath = os.path.join(TMP, "bk.zip")
    open(zpath, "wb").write(resp.content)
    check("备份打包 200 且为 ZIP", resp.status_code == 200 and resp.content[:2] == b"PK",
          str(resp.status_code))
    zf = zipfile.ZipFile(zpath)
    names = zf.namelist()
    check("备份包含清单(CSV/JSON)与恢复说明",
          {"备份清单.csv", "备份清单.json", "恢复说明.txt"} <= set(names), str(names))
    man = _json.loads(zf.read("备份清单.json").decode("utf-8"))
    check("备份清单行数与库内有效记录数一致", man["totals"]["rows"] == n_rows,
          "%s vs %s" % (man.get("totals"), n_rows))
    check("备份清单声明范围=全部期间且不含月份条件",
          "全部期间" in (man["filters"].get("scope") or "")
          and "month_from" not in man["filters"], str(man["filters"]))
    check("备份清单含 SHA256 校验值", all(len(f["sha256"]) == 16 for f in man["files"]), "")
    xs = [n for n in names if n.endswith(".xlsx")]
    check("分表命名可归档（类型_供应商_全部记录）",
          len(xs) == n_sup and all(n.startswith("历史账单_") and n.endswith("_全部记录.xlsx") for n in xs),
          str(xs))
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

    # 连续两次导出：文件名必须唯一，后者不能覆盖前者（并发导出会互相踩）
    def _dl_name(resp):
        cd = resp.headers.get("Content-Disposition", "")
        for part in cd.split(";")[1:]:
            part = part.strip()
            if part.lower().startswith("filename="):
                return part[9:].strip('"')
        return ""
    d1 = s.get(BASE + "/api/history/backup?kinds=history", timeout=60)
    d2 = s.get(BASE + "/api/history/backup?kinds=history", timeout=60)
    n1, n2 = _dl_name(d1), _dl_name(d2)
    check("连续导出文件名唯一，不覆盖上一个包",
          d1.status_code == 200 and d2.status_code == 200 and n1 and n2 and n1 != n2,
          "%s / %s" % (n1, n2))
    check("两次导出都是完整 ZIP（含清单与说明）",
          all(r.content[:2] == b"PK" for r in (d1, d2)), "%d/%d" % (len(d1.content), len(d2.content)))

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

    print("== 14b. 低数量匹配转人工核实（数量 <20 视为打样/小批量价） ==")
    # BK-1 历史：2027-01 数量 10、单价 4；BK-3 历史：2027-02 数量 20、单价 3
    b3 = [["杰盛", "2027-04-10", "BK-1", "8*8", "铜", 100, 4, 400, ""],   # 单价全等，但历史仅 10 件
          ["云锦", "2027-04-10", "BK-3", "", "塑料", 100, 3, 300, ""]]    # 历史 20 件，边界不触发
    f_b3 = make_xlsx(os.path.join(TMP, "bk_lowqty.xlsx"), b3)
    r = s.post(BASE + "/api/reconcile", files={"files": open(f_b3, "rb")}, timeout=30).json()
    c = r.get("counts", {})
    check("历史 10 件→需人工复核 / 历史 20 件→已匹配最低价",
          c.get("matched") == 1 and c.get("review") == 1, str(c))
    no3 = r["batches"][0]["recon_no"]
    res3 = s.get(BASE + "/api/results/" + no3, timeout=20).json()
    low = [x for x in res3["results"] if x["model"] == "BK-1"][0]
    edge = [x for x in res3["results"] if x["model"] == "BK-3"][0]
    check("低数量行标「历史数量偏少」并说明打样/小批量价",
          "low_qty" in (low.get("diff_types") or "") and "打样" in (low.get("reason") or ""),
          str(low)[:200])
    check("边界值 20 件不误判（仍为已匹配最低价）",
          edge.get("status") == "matched_lowest", str(edge.get("status")))
    codes3 = {d["code"] for d in (res3.get("summary") or {}).get("by_diff", [])}
    check("差异汇总出现 low_qty（历史数量偏少）", "low_qty" in codes3, str(codes3))
    rf3 = s.get(BASE + "/api/results/%s?diff=low_qty" % no3, timeout=20).json()
    check("可按该差异类型筛选（只返回 1 条）", len(rf3.get("results", [])) == 1,
          str(len(rf3.get("results", []))))

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

    # --- 16b. 型号手动查询：包含匹配 + 与其它条件叠加 + 全程只读 ---
    ev_before_q = len(s.get(BASE + "/api/suppliers/history", timeout=20).json()["events"])
    rq = s.get(BASE + "/api/suppliers/history?model=DET-1", timeout=20).json()
    check("型号查询：只返回含该型号的期间，并给出命中型号/明细数",
          rq.get("ok") is True and len(rq["rows"]) >= 1
          and all(x.get("rows_hit", 0) >= 1 and x.get("models_hit", 0) >= 1
                  and "DET-1" in (x.get("model_names") or "") for x in rq["rows"]),
          str(rq.get("summary"))[:200])
    check("型号查询：summary 回显关键字，型号数按命中口径统计",
          rq["summary"]["filter_model"] == "DET-1" and rq["summary"]["models"] >= 1
          and rq["summary"]["hit_rows"] >= 1, str(rq["summary"])[:200])
    rq2 = s.get(BASE + "/api/suppliers/history?model=det-", timeout=20).json()
    check("型号查询：包含匹配且忽略大小写（det- 命中 DET-1）",
          len(rq2["rows"]) == len(rq["rows"]), "%d vs %d" % (len(rq2["rows"]), len(rq["rows"])))
    rq3 = s.get(BASE + "/api/suppliers/history?model=%E4%B8%8D%E5%AD%98%E5%9C%A8ZZZ",
                timeout=20).json()
    check("型号查询：无匹配返回空表且不报错（不等于回退全量）",
          rq3.get("ok") is True and rq3["rows"] == [] and rq3["summary"]["models"] == 0,
          str(rq3.get("summary"))[:160])
    rq4 = s.get(BASE + "/api/suppliers/history?model=DET-1&supplier=%E4%BA%91%E9%94%A6",
                timeout=20).json()
    check("型号查询：与供应商条件叠加生效",
          len(rq4["rows"]) >= 1 and all(x["supplier"] == "云锦" for x in rq4["rows"]),
          str([(x["supplier"], x["period"]) for x in rq4["rows"]])[:200])
    rq5 = s.get(BASE + "/api/suppliers/history?model=%25", timeout=20).json()
    check("型号查询：通配符被转义（% 不当通配符用，不返回全量）",
          rq5.get("ok") is True and rq5["rows"] == [], str(len(rq5.get("rows", []))))
    ev_after_q = len(s.get(BASE + "/api/suppliers/history", timeout=20).json()["events"])
    check("型号查询全程只读：不写库、不产生查询记录/事件",
          ev_after_q == ev_before_q, "事件 %d → %d" % (ev_before_q, ev_after_q))

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

    # 失效期必须是「终态」：回归保护——曾经每次重建都会因文案里「原有效明细 N 条」
    # 由上一条的 rows_valid 变成 0 而被判成变更，反复刷 updated_at 并堆重复失效事件。
    ev_before2 = len(r4["events"])
    rows_before2 = len(r4["rows"])
    stamp_before = {x["period"]: x["updated_at"] for x in r4["rows"]}
    for _ in range(2):
        s.post(BASE + "/api/suppliers/history/rebuild", timeout=30)
    r5 = s.get(BASE + "/api/suppliers/history", timeout=20).json()
    stamp_after = {x["period"]: x["updated_at"] for x in r5["rows"]}
    check("失效期重复重建不堆事件、不改更新时间（幂等终态）",
          len(r5["events"]) == ev_before2 and len(r5["rows"]) == rows_before2
          and stamp_after == stamp_before,
          "事件 %d→%d，行数 %d→%d" % (ev_before2, len(r5["events"]),
                                        rows_before2, len(r5["rows"])))

    print("== 17. 型号明细查询与编辑（仅管理员 · 有效价明细） ==")
    _DB = os.environ.get("SMOKE_DB_PATH") or os.path.join(ROOT, "data", "recon.db")
    ed_rows = [["杰盛", "2027-07-01", "ED-1", "8*8", "铜", 10, 5, 50, ""],
               ["杰盛", "2027-07-02", "ED-2", "", "塑料", 10, 7, 70, ""]]
    f_ed = make_xlsx(os.path.join(TMP, "ed.xlsx"), ed_rows)
    r = s.post(BASE + "/api/history/upload", files={"files": open(f_ed, "rb")}, timeout=30).json()
    check("编辑用历史数据导入 ok", r.get("ok") is True and r.get("added") == 2, str(r)[:160])

    # --- 权限：非管理员既不能查也不能改，页面里也不渲染这块 ---
    s.post(BASE + "/api/auth/logout", timeout=10)
    r = s.get(BASE + "/api/admin/hist-prices?model=ED-1", timeout=20)
    check("非管理员：型号明细查询被拒 403 且提示明确",
          r.status_code == 403 and "仅管理员" in (r.json().get("error") or ""),
          "%s %s" % (r.status_code, r.text[:120]))
    r = s.post(BASE + "/api/admin/hist-prices/1", json={"unit_price": 1}, timeout=20)
    check("非管理员：保存明细被拒 403", r.status_code == 403, str(r.status_code))
    page = s.get(BASE + "/", timeout=20).text
    check("非管理员：页面源码中没有型号明细查询/编辑模块",
          'id="foldHistEdit"' not in page and 'id="edModel"' not in page
          and 'id="edBody"' not in page, "len=%d" % len(page))

    s.post(BASE + "/api/auth/login", json={"password": "13662566518"}, timeout=10)
    page = s.get(BASE + "/", timeout=20).text
    check("管理员：页面渲染出型号明细查询与编辑模块",
          'id="foldHistEdit"' in page and 'id="edModel"' in page and 'id="btnEdQuery"' in page,
          "len=%d" % len(page))

    # --- 查询：只返回有效价明细，带齐可编辑字段与来源 ---
    r = s.get(BASE + "/api/admin/hist-prices?model=ED-1", timeout=20).json()
    check("按型号查询只返回命中记录",
          r.get("ok") and r["total"] == 1 and r["rows"][0]["model"] == "ED-1", str(r)[:200])
    row = r["rows"][0]
    check("结果含可编辑字段与来源信息",
          all(k in row for k in ("id", "model", "material", "size", "period", "quantity",
                                 "total_amount", "bill_date", "remark", "unit_price",
                                 "batch_no", "source_file", "source_row", "uploaded_at")),
          str(sorted(row.keys()))[:240])
    check("空关键字被拒 400（不做全库导出）",
          s.get(BASE + "/api/admin/hist-prices?model=", timeout=20).status_code == 400)
    check("超长关键字被拒 400",
          s.get(BASE + "/api/admin/hist-prices?model=" + "A" * 61, timeout=20).status_code == 400)
    check("包含匹配且忽略大小写",
          s.get(BASE + "/api/admin/hist-prices?model=ed-", timeout=20).json()["total"] == 2)
    check("无匹配返回空集（不退化显示全部）",
          s.get(BASE + "/api/admin/hist-prices?model=ZZZ-ED", timeout=20).json()["total"] == 0)
    check("通配符 % 被当普通字符（不匹配全部）",
          s.get(BASE + "/api/admin/hist-prices?model=%25", timeout=20).json()["total"] == 0)
    n1 = s.get(BASE + "/api/status", timeout=20).json()["hist_rows"]
    s.get(BASE + "/api/admin/hist-prices?model=ED-", timeout=20)
    n2 = s.get(BASE + "/api/status", timeout=20).json()["hist_rows"]
    check("型号明细查询全程只读（历史库行数不变）", n1 == n2, "%s → %s" % (n1, n2))

    # --- 编辑：明细 + 价格基准 + 台账 同步生效 ---
    rid = row["id"]
    r = s.post(BASE + "/api/admin/hist-prices/%d" % rid, json={"unit_price": 6}, timeout=20).json()
    check("保存单价成功并回显变更字段",
          r.get("ok") is True and "单价" in (r.get("changed_fields") or []), str(r)[:200])
    r2 = s.get(BASE + "/api/admin/hist-prices?model=ED-1", timeout=20).json()["rows"][0]
    check("单价已落库（5 → 6）", abs(r2["unit_price"] - 6) < 1e-9, str(r2["unit_price"]))
    _c = sqlite3.connect(_DB)
    _pi = _c.execute("SELECT min_price FROM price_index WHERE model_norm='ED-1'").fetchone()
    _c.close()
    check("价格基准同步重建（ED-1 最低价 = 6）", _pi and abs(_pi[0] - 6) < 1e-9, str(_pi))
    sh7 = s.get(BASE + "/api/suppliers/history?supplier=%E6%9D%B0%E7%9B%9B"
                       "&month_from=2027-07&month_to=2027-07", timeout=20).json()
    check("供应商台账同步刷新（2027-07 最低价 = 6）",
          sh7["rows"] and abs(sh7["rows"][0]["min_price"] - 6) < 1e-9, str(sh7["rows"])[:200])
    check("编辑留下可追溯的状态变化记录",
          any("编辑明细" in (e["detail"] or "") for e in sh7["events"]), str(sh7["events"][:2])[:240])

    # 改期间：记录搬到另一个期间，两边台账都要跟着变
    s.post(BASE + "/api/admin/hist-prices/%d" % rid, json={"period": "2027-08"}, timeout=20)
    m7 = s.get(BASE + "/api/suppliers/history?supplier=%E6%9D%B0%E7%9B%9B"
                      "&month_from=2027-07&month_to=2027-07", timeout=20).json()["rows"]
    m8 = s.get(BASE + "/api/suppliers/history?supplier=%E6%9D%B0%E7%9B%9B"
                      "&month_from=2027-08&month_to=2027-08", timeout=20).json()["rows"]
    check("改期间后台账按新期间重算（07 剩 ED-2、08 新出现）",
          m7 and m7[0]["rows_valid"] == 1 and abs(m7[0]["min_price"] - 7) < 1e-9
          and m8 and m8[0]["rows_valid"] == 1 and abs(m8[0]["min_price"] - 6) < 1e-9,
          "07=%s 08=%s" % (m7 and m7[0]["rows_valid"], m8 and m8[0]["rows_valid"]))
    s.post(BASE + "/api/admin/hist-prices/%d" % rid, json={"period": "2027-07"}, timeout=20)

    # --- 校验：非法输入一律 400 且指出字段，不落库 ---
    def _post(payload):
        return s.post(BASE + "/api/admin/hist-prices/%d" % rid, json=payload, timeout=20)

    r = _post({"unit_price": 0})
    check("单价 0 被拒 400 并指出字段",
          r.status_code == 400 and "unit_price" in (r.json().get("errors") or {}), r.text[:160])
    r = _post({"period": "2027-13"})
    check("期间非法被拒 400", r.status_code == 400 and "period" in (r.json().get("errors") or {}),
          r.text[:160])
    r = _post({"bill_date": "2027-02-30"})
    check("不存在的日期被拒 400",
          r.status_code == 400 and "bill_date" in (r.json().get("errors") or {}), r.text[:160])
    r = _post({"model": "   "})
    check("型号为空被拒 400", r.status_code == 400 and "model" in (r.json().get("errors") or {}),
          r.text[:160])
    r = _post({"unit_price": "abc"})
    check("单价非数字被拒 400", r.status_code == 400, r.text[:120])
    r = _post({"quantity": -1})
    check("数量为负被拒 400", r.status_code == 400, r.text[:120])
    r = _post({})
    check("内容未变化时拒绝保存 400",
          r.status_code == 400 and "没有变化" in (r.json().get("error") or ""), r.text[:160])
    r = s.post(BASE + "/api/admin/hist-prices/99999999", json={"unit_price": 1}, timeout=20)
    check("不存在的记录 404", r.status_code == 404, str(r.status_code))
    r = s.post(BASE + "/api/admin/hist-prices/%d" % rid, data="notjson",
               headers={"Content-Type": "application/json"}, timeout=20)
    check("非 JSON 请求体被拒 400", r.status_code == 400, str(r.status_code))
    r = _post({"supplier": "云锦", "remark": "越权测试"})
    check("供应商不可通过编辑接口改动（只改了备注）",
          r.status_code == 200 and r.json()["row"]["supplier"] == "杰盛"
          and "供应商" not in (r.json().get("changed_fields") or []), r.text[:200])
    _post({"remark": ""})          # 还原备注，便于后续「未变化」与污染检查
    after = s.get(BASE + "/api/admin/hist-prices?model=ED-1", timeout=20).json()["rows"][0]
    check("非法请求未污染数据（供应商/单价保持原值）",
          after["supplier"] == "杰盛" and abs(after["unit_price"] - 6) < 1e-9, str(after)[:200])

    # 已排除明细不在「有效数据」范围内，不允许在此编辑
    _c = sqlite3.connect(_DB)
    _c.execute("INSERT INTO hist_price(batch_no,supplier,model,model_norm,period,unit_price,"
               "exclude_reason,dedup_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
               ("ED-EX", "杰盛", "ED-EX", "ED-EX", "2027-09", None, "待核价",
                "smoke-excluded-fixture", "2027-09-01 00:00:00"))
    _c.commit()
    ex_id = _c.execute("SELECT id FROM hist_price WHERE dedup_hash='smoke-excluded-fixture'"
                       ).fetchone()[0]
    _c.close()
    r = s.post(BASE + "/api/admin/hist-prices/%d" % ex_id, json={"unit_price": 3}, timeout=20)
    check("已排除明细不允许在有效数据中编辑（409）",
          r.status_code == 409 and "排除" in (r.json().get("error") or ""), r.text[:160])
    _c = sqlite3.connect(_DB)
    _c.execute("DELETE FROM hist_price WHERE id=?", (ex_id,))
    _c.commit()
    dup = _c.execute("SELECT COUNT(*), COUNT(DISTINCT dedup_hash) FROM hist_price").fetchone()
    _c.close()
    check("多次编辑后去重哈希仍唯一（不撞唯一索引）", dup[0] == dup[1], str(dup))
    s.post(BASE + "/api/suppliers/history/rebuild", timeout=30)

    print()
    print("=" * 48)
    print("冒烟测试结果：通过 %d 项 / 失败 %d 项" % (PASS, FAIL))
    print("（登录锁定 10 分钟后自动解除，或重启服务清零）")
    print("=" * 48)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
