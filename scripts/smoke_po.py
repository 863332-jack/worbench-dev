# -*- coding: utf-8 -*-
"""采购工程流程 · 冒烟测试。

覆盖：
1. 未登录拦截（401）
2. 采购方登录 → 上传图纸下单（待确认）
3. 供应商可见性隔离（本家可见 / 他家 403 且列表不含）
4. 供应商确认接单（状态机 pending→confirmed，重复确认 409）
5. 供应商越权收货（403）
6. 采购方登记收货 + 凭证（confirmed→received，重复收货 409）
7. 进度轨迹事件完整性（create/confirm/receive）
8. 图纸/凭证下载（内容校验）
9. 表单校验（缺图纸/数量 0/错误格式 400）
10. 拒接分支（pending→rejected，之后不能收货）

用法：服务启动后执行  python3 scripts/smoke_po.py
测试数据以 SMOKE- 前缀标记，结束后自动清理，可重复运行。
"""
import os
import sys
import sqlite3
import shutil

import requests

BASE = "http://127.0.0.1:5088"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(ROOT, "scripts", ".smoke_po_tmp")

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
    # 1x1 透明 PNG
    with open(path, "wb") as f:
        f.write(bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c6360000000020001e221bc330000000049454e44ae426082"))
    return path


def create_order(sess, product, supplier="杰盛", qty="300", price="12.5", drawing=True):
    data = {"supplier": supplier, "product_name": product, "model": "M-01",
            "quantity": qty, "unit": "件", "unit_price": price,
            "required_date": "2026-12-31", "remark": "SMOKE 测试订单"}
    files = {}
    if drawing:
        files["drawing"] = ("drawing.pdf", open(os.path.join(TMP, "drawing.pdf"), "rb"), "application/pdf")
    return sess.post(BASE + "/api/po/orders", data=data, files=files, timeout=20)


def main():
    os.makedirs(TMP, exist_ok=True)
    make_pdf(os.path.join(TMP, "drawing.pdf"))
    make_png(os.path.join(TMP, "receipt.png"))

    buyer = requests.Session()
    sh_jiesheng = requests.Session()
    sh_xinxin = requests.Session()
    sh_yunjin = requests.Session()

    print("== 1. 未登录拦截 ==")
    r = buyer.get(BASE + "/api/po/orders", timeout=10)
    check("未登录列表 401", r.status_code == 401, "code=%s" % r.status_code)
    r = buyer.post(BASE + "/api/po/orders", data={"supplier": "杰盛"}, timeout=10)
    check("未登录下单 401", r.status_code == 401, "code=%s" % r.status_code)

    print("== 2. 登录（三角色） ==")
    r = buyer.post(BASE + "/api/po/login", json={"username": "purchaser", "password": "123456"}, timeout=10)
    check("采购方登录", r.status_code == 200 and r.json().get("ok"), str(r)[:120])
    r = sh_jiesheng.post(BASE + "/api/po/login", json={"username": "jiesheng", "password": "123456"}, timeout=10)
    check("供应商(杰盛)登录", r.status_code == 200 and r.json()["user"]["supplier"] == "杰盛")
    r = sh_xinxin.post(BASE + "/api/po/login", json={"username": "xinxin", "password": "123456"}, timeout=10)
    check("供应商(新新精密)登录", r.status_code == 200)
    r = sh_yunjin.post(BASE + "/api/po/login", json={"username": "yunjin", "password": "123456"}, timeout=10)
    check("供应商(云锦)登录", r.status_code == 200)
    r = requests.post(BASE + "/api/po/login", json={"username": "purchaser", "password": "bad"}, timeout=10)
    check("错误密码 403", r.status_code == 403)

    print("== 3. 采购方上传图纸下单 ==")
    r = create_order(buyer, "SMOKE-铜接头")
    check("下单成功 200", r.status_code == 200 and r.json().get("ok"), str(r)[:200])
    order = r.json()["order"]
    no1 = order["order_no"]
    check("初始状态=待确认", order["status"] == "pending_confirm", order["status"])
    check("金额自动计算=3750", order["total_amount"] == 3750, str(order["total_amount"]))
    check("图纸已保存", bool(order["drawing_path"]) and order["drawing_name"] == "drawing.pdf")

    print("== 4. 表单校验 ==")
    r = create_order(buyer, "SMOKE-无图纸", drawing=False)
    check("缺图纸 400", r.status_code == 400 and "图纸" in str(r.json().get("errors")))
    r = create_order(buyer, "SMOKE-零数量", qty="0")
    check("数量 0 被拒 400", r.status_code == 400)

    print("== 5. 供应商可见性隔离 ==")
    r = sh_jiesheng.get(BASE + "/api/po/orders", timeout=10).json()
    check("本家供应商可见该单", any(o["order_no"] == no1 for o in r["orders"]))
    r = sh_xinxin.get(BASE + "/api/po/orders", timeout=10).json()
    check("他家供应商列表不含该单", all(o["order_no"] != no1 for o in r["orders"]))
    r = sh_xinxin.get(BASE + "/api/po/orders/" + no1, timeout=10)
    check("他家供应商详情 403", r.status_code == 403)

    print("== 6. 供应商确认接单 ==")
    r = sh_jiesheng.post(BASE + "/api/po/orders/" + no1 + "/confirm", timeout=10)
    check("确认接单 200", r.status_code == 200 and r.json()["order"]["status"] == "confirmed", str(r)[:150])
    check("确认人已记录", r.json()["order"]["confirmed_by"] != "")
    r = sh_jiesheng.post(BASE + "/api/po/orders/" + no1 + "/confirm", timeout=10)
    check("重复确认 409", r.status_code == 409, "code=%s" % r.status_code)

    print("== 7. 角色权限 ==")
    r = sh_jiesheng.post(BASE + "/api/po/orders/" + no1 + "/receive",
                         data={"received_qty": "300", "received_date": "2026-12-01"}, timeout=10)
    check("供应商越权收货 403", r.status_code == 403, "code=%s" % r.status_code)
    r = sh_xinxin.post(BASE + "/api/po/orders/" + no1 + "/confirm", timeout=10)
    check("他家供应商确认 403", r.status_code == 403, "code=%s" % r.status_code)
    r = buyer.post(BASE + "/api/po/orders/" + no1 + "/confirm", timeout=10)
    check("采购方不能替供应商确认 403", r.status_code == 403, "code=%s" % r.status_code)

    print("== 8. 采购方登记收货 + 凭证 ==")
    r = buyer.post(BASE + "/api/po/orders/" + no1 + "/receive",
                   data={"received_qty": "300", "received_date": "2026-12-01",
                         "receive_remark": "外包装完好"},
                   files={"receipt": ("receipt.png", open(os.path.join(TMP, "receipt.png"), "rb"), "image/png")},
                   timeout=20)
    check("收货登记 200", r.status_code == 200 and r.json()["order"]["status"] == "received", str(r)[:150])
    check("到货数量/日期已记录",
          r.json()["order"]["received_qty"] == 300 and r.json()["order"]["received_date"] == "2026-12-01")
    check("收货凭证已保存", r.json()["order"]["receipt_name"] == "receipt.png")
    r = buyer.post(BASE + "/api/po/orders/" + no1 + "/receive",
                   data={"received_qty": "1", "received_date": "2026-12-02"}, timeout=10)
    check("重复收货 409（已收货不可再收）", r.status_code == 409, "code=%s" % r.status_code)

    print("== 9. 进度轨迹完整性 ==")
    r = buyer.get(BASE + "/api/po/orders/" + no1, timeout=10).json()
    acts = [e["action"] for e in r["events"]]
    check("事件序列 create→confirm→receive", acts == ["create", "confirm", "receive"], str(acts))
    check("终态=已收货", r["order"]["status"] == "received")

    print("== 10. 附件下载 ==")
    r = sh_jiesheng.get(BASE + "/api/po/orders/" + no1 + "/file?type=drawing", timeout=10)
    check("供应商下载图纸 200 + PDF", r.status_code == 200 and r.content[:4] == b"%PDF")
    r = buyer.get(BASE + "/api/po/orders/" + no1 + "/file?type=receipt", timeout=10)
    check("采购方下载凭证 200 + PNG", r.status_code == 200 and r.content[:4] == b"\x89PNG")
    r = sh_xinxin.get(BASE + "/api/po/orders/" + no1 + "/file?type=drawing", timeout=10)
    check("他家供应商下载 403", r.status_code == 403)

    print("== 11. 拒接分支 ==")
    r = create_order(buyer, "SMOKE-将被拒接")
    no2 = r.json()["order"]["order_no"]
    r = sh_jiesheng.post(BASE + "/api/po/orders/" + no2 + "/reject",
                         json={"reason": "产能排满，无法接单"}, timeout=10)
    check("拒接 200 + 状态=已拒接", r.status_code == 200 and r.json()["order"]["status"] == "rejected")
    check("拒接原因已记录", r.json()["order"]["reject_reason"] == "产能排满，无法接单")
    r = buyer.post(BASE + "/api/po/orders/" + no2 + "/receive",
                   data={"received_qty": "1", "received_date": "2026-12-03"}, timeout=10)
    check("拒接单不能收货 409", r.status_code == 409, "code=%s" % r.status_code)

    print("== 12. 清理测试数据 ==")
    conn = sqlite3.connect(os.path.join(ROOT, "data", "recon.db"))
    rows = conn.execute("SELECT order_no FROM po_order WHERE product_name LIKE 'SMOKE-%'").fetchall()
    for (ono,) in rows:
        conn.execute("DELETE FROM po_event WHERE order_no=?", (ono,))
        conn.execute("DELETE FROM po_order WHERE order_no=?", (ono,))
        shutil.rmtree(os.path.join(ROOT, "uploads", "po", ono), ignore_errors=True)
    conn.commit()
    left = conn.execute("SELECT COUNT(*) FROM po_order WHERE product_name LIKE 'SMOKE-%'").fetchone()[0]
    conn.close()
    check("测试订单已清理", left == 0, "remaining=%d" % left)

    print()
    print("=" * 48)
    print("采购流程冒烟测试：通过 %d 项 / 失败 %d 项" % (PASS, FAIL))
    print("=" * 48)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
