# -*- coding: utf-8 -*-
"""采购工程流程 · 冒烟测试（多行下单版）。

覆盖：
1. 未登录拦截 / 三角色登录 / 权限隔离
2. 型号匹配三态：单档匹配回填、多档价提示、未找到提示（含跨供应商不可见）
2b. 型号等价写法（大小写/全角/空格/连字符/零宽字符/乘号）、近似匹配、跨供应商诊断
3. 多行下单（1-50 行）、行级图纸、行数上限 51 拒绝、必填行号反馈
3b. 多格式图纸（step/docx/tif）上传下载、非法扩展名拒绝
4. 确认接单 → 登记收货 → 事件轨迹（回归）
5. 行级图纸下载（供应商端）+ 越权 403
6. 拒接分支
7. 清理测试数据（订单/历史库/附件）

用法：服务启动后执行  python3 scripts/smoke_po.py
"""
import os
import sys
import json
import sqlite3
import shutil

import requests
from openpyxl import Workbook

BASE = "http://127.0.0.1:5088"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(ROOT, "scripts", ".smoke_po_tmp")
HEADERS = ["供应商 *", "日期 *", "型号 *", "尺寸", "材质", "数量", "单价 *", "合计金额", "备注"]

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % name)
    else:
        FAIL += 1
        print("  ✗ %s  %s" % (name, detail))


def make_pdf(path):
    with open(path, "wb") as f:
        f.write(b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")
    return path


def make_png(path):
    with open(path, "wb") as f:
        f.write(bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c6360000000020001e221bc330000000049454e44ae426082"))
    return path


def make_step(path):
    """伪造 STEP 三维模型文件（扩展名校验用）。"""
    with open(path, "wb") as f:
        f.write(b"ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('SMOKE'),'2;1');\nENDSEC;\n"
                b"DATA;\nENDSEC;\nEND-ISO-10303-21;\n")
    return path


def make_docx(path):
    """伪造 Word 图纸说明文档（扩展名校验用，内容为最小 docx 结构）。"""
    import zipfile
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
    return path


def make_tif(path):
    """伪造 TIFF 扫描图（扩展名校验用）。"""
    with open(path, "wb") as f:
        f.write(b"II*\x00\x08\x00\x00\x00\x00\x00")
    return path


def make_hist_xlsx(path):
    """构造历史账单：
    杰盛 J55(4) / J83(3.5)；
    新新精密 M12 同规格两档价(5,7)；LSS-01 同型号两种规格(尺寸/材质/价均不同)；
    云锦 NP-50(6)。
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(HEADERS)
    rows = [
        ["杰盛", "2026-07-03", "J55", "15*8", "铜", 300, 4, 1200, ""],
        ["杰盛", "2026-07-03", "J83", "20*10", "铜", 500, 3.5, 1750, ""],
        ["新新精密", "2026-08-20", "M12", "D12*40", "铝", 80, 5, 400, ""],
        ["新新精密", "2026-09-20", "M12", "D12*40", "铝", 80, 7, 560, ""],
        ["新新精密", "2026-08-10", "LSS-01", "D20*30", "钢", 50, 8, 400, ""],
        ["新新精密", "2026-09-10", "LSS-01", "D20*50", "铜", 30, 9, 270, ""],
        ["云锦", "2026-08-05", "NP-50", "50*20", "塑料", 100, 6, 600, ""],
    ]
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


def items_payload(n, supplier="杰盛", model="J55"):
    return [{"line_no": i + 1, "model": ("%s-%d" % (model, i + 1)) if i else model,
             "param": "", "size": "", "material": "", "quantity": 10, "unit": "件",
             "unit_price": 4, "required_date": "2026-12-31", "remark": ""} for i in range(n)]


def guard_live_data():
    """安全闸门：冒烟测试会导入并**清空**历史库、删除测试订单。

    2026-11-26 起系统已在局域网实际使用（真实订单/历史数据可能已存在），
    因此默认拒绝在有任何业务数据时运行；确需运行时显式设置 SMOKE_FORCE=1。
    """
    import sqlite3
    db_path = os.path.join(ROOT, "data", "recon.db")
    if not os.path.exists(db_path):
        return
    conn = sqlite3.connect(db_path)
    busy = {}
    for t in ("hist_price", "recon_batch", "po_order"):
        try:
            busy[t] = conn.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]
        except sqlite3.Error:
            busy[t] = 0
    conn.close()
    if any(v for v in busy.values()) and os.environ.get("SMOKE_FORCE") != "1":
        print("=" * 60)
        print("已中止：数据库中存在业务数据 %s" % busy)
        print("冒烟测试会导入并清空历史库、删除测试订单，为避免破坏真实数据默认不执行。")
        print("确认可以清空时：先备份 data/recon.db，再用 SMOKE_FORCE=1 重新运行。")
        print("=" * 60)
        raise SystemExit(2)


def main():
    guard_live_data()
    os.makedirs(TMP, exist_ok=True)
    make_pdf(os.path.join(TMP, "drawing.pdf"))
    make_png(os.path.join(TMP, "receipt.png"))
    hist_file = make_hist_xlsx(os.path.join(TMP, "hist.xlsx"))

    buyer = requests.Session()
    sh_jiesheng = requests.Session()
    sh_xinxin = requests.Session()
    admin = requests.Session()

    print("== 1. 未登录拦截与登录 ==")
    check("未登录列表 401", buyer.get(BASE + "/api/po/orders", timeout=10).status_code == 401)
    check("未登录匹配 401", buyer.get(BASE + "/api/po/model-lookup?supplier=杰盛&model=J55",
                                     timeout=10).status_code == 401)
    r = buyer.post(BASE + "/api/po/login", json={"username": "purchaser", "password": "123456"}, timeout=10)
    check("采购方登录", r.status_code == 200 and r.json().get("ok"))
    r = sh_jiesheng.post(BASE + "/api/po/login", json={"username": "jiesheng", "password": "123456"}, timeout=10)
    check("供应商(杰盛)登录", r.status_code == 200 and r.json()["user"]["supplier"] == "杰盛")
    r = sh_xinxin.post(BASE + "/api/po/login", json={"username": "xinxin", "password": "123456"}, timeout=10)
    check("供应商(新新精密)登录", r.status_code == 200)
    r = admin.post(BASE + "/api/auth/login", json={"password": "13662566518"}, timeout=10)
    check("管理员登录（准备历史数据）", r.status_code == 200)

    print("== 2. 准备历史账单（供型号匹配） ==")
    r = admin.post(BASE + "/api/history/upload",
                   files={"files": ("hist.xlsx", open(hist_file, "rb"))}, timeout=30).json()
    check("历史数据导入 7 条", r.get("ok") and r.get("added") == 7, str(r)[:200])

    print("== 3. 型号匹配：自动填充与候选 ==")
    r = buyer.get(BASE + "/api/po/model-lookup?supplier=杰盛&model=J55", timeout=10).json()
    check("J55 匹配成功并回填", r.get("ok") and r.get("found") and r.get("size") == "15*8"
          and r.get("material") == "铜" and r.get("unit_price") == 4, str(r)[:200])
    check("J55 单档不告警", r.get("risk") is False)
    # 自动填充必须带「来源」，否则无法复核
    auto = r.get("auto") or {}
    src = ((auto.get("size") or {}).get("source") or {})
    check("自动填充字段带数据来源（账期/供应商/文件名·行号）",
          all(k in auto for k in ("size", "material", "unit_price"))
          and "2026-07" in src.get("text", "") and "杰盛" in src.get("text", "")
          and "hist.xlsx" in src.get("text", "") and "行" in src.get("text", ""),
          str(auto)[:300])
    check("唯一口径时无歧义字段", (r.get("ambiguous") or []) == [], str(r.get("ambiguous")))
    # 同规格两档价：尺寸/材质唯一可填，单价不唯一必须人工选
    r = buyer.get(BASE + "/api/po/model-lookup?supplier=新新精密&model=M12", timeout=10).json()
    check("M12 多档价提示 risk", r.get("found") and r.get("risk") is True, str(r)[:200])
    check("M12 尺寸/材质唯一仍自动填充",
          r.get("size") == "D12*40" and r.get("material") == "铝", str(r)[:200])
    check("M12 单价不唯一时不自动填充",
          r.get("unit_price") is None and "unit_price" in (r.get("ambiguous") or [])
          and "unit_price" not in (r.get("auto") or {}), str(r)[:250])
    check("多档价提示含各档金额", "¥5" in (r.get("message") or "") and "¥7" in (r.get("message") or ""),
          r.get("message", ""))
    check("M12 返回 2 条候选且候选带来源",
          len(r.get("candidates") or []) == 2
          and all(c.get("source", {}).get("text") for c in r["candidates"]), str(r.get("candidates"))[:250])
    # 同型号两种规格：尺寸/材质/单价全不唯一 → 一律不填，只给候选
    r = buyer.get(BASE + "/api/po/model-lookup?supplier=新新精密&model=LSS-01", timeout=10).json()
    check("LSS-01 多规格时尺寸/材质均不自动填充",
          r.get("size") == "" and r.get("material") == ""
          and set(r.get("ambiguous") or []) >= {"size", "material", "unit_price"}, str(r)[:250])
    check("LSS-01 候选含两种规格",
          sorted({(c.get("size"), c.get("material")) for c in r.get("candidates") or []})
          == [("D20*30", "钢"), ("D20*50", "铜")], str(r.get("candidates"))[:250])
    # 跨供应商 & 未命中
    r = buyer.get(BASE + "/api/po/model-lookup?supplier=杰盛&model=M12", timeout=10).json()
    check("跨供应商不可见（杰盛查 M12 未命中）", r.get("ok") and r.get("found") is False)
    r = buyer.get(BASE + "/api/po/model-lookup?supplier=杰盛&model=ZZ-999", timeout=10).json()
    check("未命中给出明确提示", r.get("found") is False
          and "历史账单中没有" in (r.get("message") or ""), r.get("message", ""))
    r = buyer.get(BASE + "/api/po/model-lookup?supplier=&model=J55", timeout=10)
    check("未选供应商时匹配被拒 400", r.status_code == 400)

    print("== 3b. 型号等价写法与近似匹配（缺陷 P1-5 回归） ==")
    # 历史库中的 J55 是「J55」写法，以下都是真人/Excel 最常带出来的等价写法
    for typed, label in [("j55", "小写"), ("Ｊ５５", "全角"), ("J55 ", "尾部空格"),
                         ("J55-", "尾部连字符"), ("J55\u200b", "零宽字符")]:
        r = buyer.get(BASE + "/api/po/model-lookup",
                      params={"supplier": "杰盛", "model": typed}, timeout=10).json()
        check("等价写法命中：%s" % label,
              r.get("found") is True and r.get("match_type") == "exact"
              and r.get("unit_price") == 4, "%r -> %s" % (typed, str(r)[:180]))
    # 历史 M12 存在多档价，用乘号/小数点变体也要精确命中
    for typed, label in [("m12x40", "乘号 x"), ("M12×40", "乘号 ×")]:
        r = buyer.get(BASE + "/api/po/model-lookup",
                      params={"supplier": "新新精密", "model": typed}, timeout=10).json()
        check("分隔符归一：%s" % label, r.get("found") is True, "%r -> %s" % (typed, str(r)[:180]))
    r = buyer.get(BASE + "/api/po/model-lookup?supplier=杰盛&model=1002.0", timeout=10).json()
    check("数字型号尾随 .0 归一（无此型号时不误判异常）", r.get("found") is False, str(r)[:150])
    # 相近型号：不自动填充任何字段，只给候选供选择
    r = buyer.get(BASE + "/api/po/model-lookup?supplier=杰盛&model=J55X", timeout=10).json()
    check("相近型号不自动填充（尺寸/材质/单价全留空）",
          r.get("found") is True and r.get("match_type") == "prefix"
          and r.get("size") == "" and r.get("material") == ""
          and r.get("unit_price") is None and (r.get("auto") or {}) == {}, str(r)[:260])
    check("相近型号给出候选且标明命中型号",
          len(r.get("candidates") or []) == 1 and r.get("matched_model") == "J55"
          and r["candidates"][0].get("source", {}).get("text"), str(r.get("candidates"))[:200])
    check("相近型号提示需人工选择", "选择" in (r.get("message") or ""), r.get("message", ""))
    # 跨供应商诊断：杰盛查 M12 → 明确指出在新新精密存在
    r = buyer.get(BASE + "/api/po/model-lookup?supplier=杰盛&model=M12", timeout=10).json()
    check("跨供应商诊断指出正确供应商",
          r.get("found") is False and "新新精密" in (r.get("supplier_hint") or ""), str(r)[:200])
    r = buyer.get(BASE + "/api/po/suppliers", timeout=10).json()
    check("供应商列表接口可用", r.get("ok") and "杰盛" in (r.get("suppliers") or []), str(r)[:150])

    print("== 4. 多行下单（3 行，2 行带图纸） ==")
    items = [
        {"line_no": 1, "model": "J55", "param": "1 档价 ¥4", "size": "15*8", "material": "铜",
         "quantity": 300, "unit": "件", "unit_price": 4, "required_date": "2026-12-15", "remark": "SMOKE-表面镀镍"},
        {"line_no": 2, "model": "J83", "param": "", "size": "20*10", "material": "铜",
         "quantity": 500, "unit": "件", "unit_price": 3.5, "required_date": "2026-12-20", "remark": "SMOKE-无图纸行"},
        {"line_no": 3, "model": "ZZ-999", "param": "", "size": "手工尺寸", "material": "手工材料",
         "quantity": 20, "unit": "件", "unit_price": None, "required_date": "2026-12-25", "remark": "SMOKE-未匹配型号"},
    ]
    data = {"supplier": "杰盛", "remark": "SMOKE 多行测试单", "items_json": json.dumps(items, ensure_ascii=False)}
    files = {
        "drawing_1": ("line1.pdf", open(os.path.join(TMP, "drawing.pdf"), "rb"), "application/pdf"),
        "drawing_3": ("line3.pdf", open(os.path.join(TMP, "drawing.pdf"), "rb"), "application/pdf"),
    }
    r = buyer.post(BASE + "/api/po/orders", data=data, files=files, timeout=30).json()
    check("多行下单成功", r.get("ok") is True, str(r)[:250])
    order = r.get("order") or {}
    no1 = order.get("order_no", "")
    check("订单头汇总：3 项 / 总数量 820 / 金额 2950",
          order.get("product_name") == "多产品订单（3 项）"
          and order.get("quantity") == 820 and order.get("total_amount") == 2950,
          "%s qty=%s amt=%s" % (order.get("product_name"), order.get("quantity"), order.get("total_amount")))
    check("初始状态=待确认", order.get("status") == "pending_confirm")

    print("== 4b. 多格式图纸上传与下载（缺陷 P1-7 回归） ==")
    make_step(os.path.join(TMP, "m.step"))
    make_docx(os.path.join(TMP, "spec.docx"))
    make_tif(os.path.join(TMP, "scan.tif"))
    fmt_items = [
        {"line_no": 1, "model": "J55", "quantity": 5, "unit": "件",
         "required_date": "2026-12-31", "remark": "SMOKE-STEP"},
        {"line_no": 2, "model": "J83", "quantity": 5, "unit": "件",
         "required_date": "2026-12-31", "remark": "SMOKE-DOCX"},
        {"line_no": 3, "model": "J83", "quantity": 5, "unit": "件",
         "required_date": "2026-12-31", "remark": "SMOKE-TIF"},
    ]
    r = buyer.post(
        BASE + "/api/po/orders",
        data={"supplier": "杰盛", "remark": "SMOKE 多格式图纸",
              "items_json": json.dumps(fmt_items, ensure_ascii=False)},
        files={
            "drawing_1": ("模型.step", open(os.path.join(TMP, "m.step"), "rb"), "application/octet-stream"),
            "drawing_2": ("图纸说明.docx", open(os.path.join(TMP, "spec.docx"), "rb"),
                          "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            "drawing_3": ("扫描件.tif", open(os.path.join(TMP, "scan.tif"), "rb"), "image/tiff"),
        },
        timeout=30).json()
    check("step / docx / tif 三种格式均被接收", r.get("ok") is True, str(r)[:250])
    no3 = (r.get("order") or {}).get("order_no", "")
    if no3:
        detail = buyer.get(BASE + "/api/po/orders/" + no3, timeout=10).json()
        names = [it.get("drawing_name") for it in detail.get("items") or []]
        check("三行图纸文件名原样保留", names == ["模型.step", "图纸说明.docx", "扫描件.tif"], str(names))
        for line, ext, head in [(1, ".step", b"ISO-10303"), (2, ".docx", b"PK"),
                                (3, ".tif", b"II*\x00")]:
            rr = sh_jiesheng.get(BASE + "/api/po/orders/" + no3 + "/file?type=drawing&line=%d" % line,
                                 timeout=10)
            cd = rr.headers.get("Content-Disposition", "")
            ok = (rr.status_code == 200 and rr.content[:len(head)] == head
                  and ext in cd.lower())
            check("第 %d 行 %s 可下载且文件名带扩展名" % (line, ext), ok,
                  "status=%s cd=%s" % (rr.status_code, cd))
        rr = buyer.post(BASE + "/api/po/orders",
                        data={"supplier": "杰盛", "items_json": json.dumps([fmt_items[0]], ensure_ascii=False)},
                        files={"drawing_1": ("bad.exe", b"MZ", "application/octet-stream")}, timeout=20)
        check("非法扩展名 .exe 仍被拒 400",
              rr.status_code == 400 and any("图纸格式" in e for e in rr.json().get("errors", [])),
              str(rr.json())[:200])
    else:
        no3 = ""

    print("== 5. 行数上限与必填校验 ==")
    r = buyer.post(BASE + "/api/po/orders",
                   data={"supplier": "杰盛", "items_json": json.dumps(items_payload(51), ensure_ascii=False)},
                   timeout=20)
    check("51 行被拒 400", r.status_code == 400 and "50" in r.json().get("error", ""), str(r)[:160])
    bad = [{"line_no": 1, "model": "J55", "quantity": 1, "required_date": "2026-12-31"},
           {"line_no": 2, "model": "", "quantity": 1, "required_date": "2026-12-31"}]
    r = buyer.post(BASE + "/api/po/orders",
                   data={"supplier": "杰盛", "items_json": json.dumps(bad, ensure_ascii=False)},
                   files={"drawing_1": ("d.pdf", open(os.path.join(TMP, "drawing.pdf"), "rb"), "application/pdf")},
                   timeout=20)
    body = r.json()
    check("必填缺失 400 且指出行号", r.status_code == 400
          and any("第 2 行" in e and "型号" in e for e in body.get("errors", [])), str(body)[:250])
    r = buyer.post(BASE + "/api/po/orders",
                   data={"supplier": "杰盛", "items_json": json.dumps([items[0]], ensure_ascii=False)},
                   timeout=20)
    check("无图纸被拒 400", r.status_code == 400
          and any("图纸" in e for e in r.json().get("errors", [])), str(r.json())[:200])

    print("== 6. 详情产品行与行级图纸 ==")
    r = buyer.get(BASE + "/api/po/orders/" + no1, timeout=10).json()
    its = r.get("items") or []
    check("详情返回 3 行产品", len(its) == 3, "items=%d" % len(its))
    check("第 1 行图纸已关联", its[0].get("drawing_name") == "line1.pdf")
    check("第 2 行无图纸", not its[1].get("drawing_name"))
    check("第 3 行图纸已关联", its[2].get("drawing_name") == "line3.pdf")
    check("特殊备注按行保存", its[0].get("remark") == "SMOKE-表面镀镍")

    print("== 7. 供应商可见性与行级下载 ==")
    r = sh_jiesheng.get(BASE + "/api/po/orders/" + no1, timeout=10)
    check("本家供应商可见详情", r.status_code == 200 and len(r.json().get("items") or []) == 3)
    r = sh_jiesheng.get(BASE + "/api/po/orders/" + no1 + "/file?type=drawing&line=1", timeout=10)
    check("供应商下载第 1 行图纸 200 + PDF", r.status_code == 200 and r.content[:4] == b"%PDF")
    r = sh_xinxin.get(BASE + "/api/po/orders/" + no1, timeout=10)
    check("他家供应商详情 403", r.status_code == 403)
    r = sh_xinxin.get(BASE + "/api/po/orders/" + no1 + "/file?type=drawing&line=1", timeout=10)
    check("他家供应商下载 403", r.status_code == 403)
    r = buyer.get(BASE + "/api/po/orders/" + no1 + "/file?type=drawing&line=2", timeout=10)
    check("无图纸行下载 404", r.status_code == 404)

    print("== 8. 确认 → 收货（回归） ==")
    r = sh_jiesheng.post(BASE + "/api/po/orders/" + no1 + "/confirm", timeout=10)
    check("供应商确认接单", r.status_code == 200 and r.json()["order"]["status"] == "confirmed")
    r = sh_jiesheng.post(BASE + "/api/po/orders/" + no1 + "/confirm", timeout=10)
    check("重复确认 409", r.status_code == 409)
    r = buyer.post(BASE + "/api/po/orders/" + no1 + "/receive",
                   data={"received_qty": "820", "received_date": "2026-12-16", "receive_remark": "SMOKE 收货"},
                   files={"receipt": ("receipt.png", open(os.path.join(TMP, "receipt.png"), "rb"), "image/png")},
                   timeout=20)
    check("采购方登记收货", r.status_code == 200 and r.json()["order"]["status"] == "received")
    r = buyer.get(BASE + "/api/po/orders/" + no1, timeout=10).json()
    check("事件序列 create→confirm→receive",
          [e["action"] for e in r["events"]] == ["create", "confirm", "receive"])
    r = sh_jiesheng.post(BASE + "/api/po/orders/" + no1 + "/receive",
                         data={"received_qty": "1", "received_date": "2026-12-16"}, timeout=10)
    check("供应商越权收货 403", r.status_code == 403)

    print("== 9. 拒接分支 ==")
    r = buyer.post(BASE + "/api/po/orders",
                   data={"supplier": "杰盛", "items_json": json.dumps([items[0]], ensure_ascii=False)},
                   files={"drawing_1": ("d.pdf", open(os.path.join(TMP, "drawing.pdf"), "rb"), "application/pdf")},
                   timeout=20).json()
    no2 = r["order"]["order_no"]
    r = sh_jiesheng.post(BASE + "/api/po/orders/" + no2 + "/reject",
                         json={"reason": "SMOKE 产能不足"}, timeout=10)
    check("拒接 200 + 状态已拒接", r.status_code == 200 and r.json()["order"]["status"] == "rejected")

    print("== 10. 清理测试数据 ==")
    conn = sqlite3.connect(os.path.join(ROOT, "data", "recon.db"))
    order_nos = [n for n in [no1, no2, no3] if n]
    for ono in order_nos:
        conn.execute("DELETE FROM po_order_item WHERE order_no=?", (ono,))
        conn.execute("DELETE FROM po_event WHERE order_no=?", (ono,))
        conn.execute("DELETE FROM po_order WHERE order_no=?", (ono,))
        shutil.rmtree(os.path.join(ROOT, "uploads", "po", ono), ignore_errors=True)
    conn.commit()
    left = 0
    for ono in order_nos:
        left += conn.execute("SELECT COUNT(*) FROM po_order WHERE order_no=?", (ono,)).fetchone()[0]
    conn.close()
    check("测试订单已清理", left == 0, "remaining=%d" % left)
    # 清空历史库，恢复干净初始态
    r = admin.post(BASE + "/api/history/reset", timeout=30)
    check("历史库已清空", r.status_code == 200 and r.json().get("ok"))
    for f in os.listdir(os.path.join(ROOT, "data", "backups")):
        try:
            os.remove(os.path.join(ROOT, "data", "backups", f))
        except OSError:
            pass
    shutil.rmtree(TMP, ignore_errors=True)   # 不留临时文件

    print()
    print("=" * 48)
    print("采购流程冒烟测试：通过 %d 项 / 失败 %d 项" % (PASS, FAIL))
    print("=" * 48)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
