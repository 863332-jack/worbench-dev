# -*- coding: utf-8 -*-
"""账单备份：按「账单类型 + 供应商」分类，把该供应商的**全部历史记录**打包成可归档、
可回灌的 ZIP。不支持按月筛选——文件粒度只能是供应商。

设计要点：
1. **可恢复优先**：每个分表都按标准录入模板的 9 列输出，解压后可原样从入口① 重新上传，
   用于恢复历史价基准（重复数据按 dedup 自动跳过，重复导入安全）；
2. **命名可归档**：ZIP 与分表文件名都带类型/供应商，按文件名即可判断内容；
3. **可核对**：ZIP 内附 `备份清单.csv`（人读）与 `备份清单.json`（机读，含行数与 SHA256），
   以及 `恢复说明.txt`。
"""
import os
import re
import csv
import json
import hashlib
import zipfile
from datetime import datetime
from io import StringIO

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

import db
from config import OUTPUT_DIR

# 与 deliverable 模板一致的 9 列（带 * 为必填），保证导出的分表能被入口① 直接回灌
BACKUP_HEADERS = ["供应商 *", "日期 *", "型号 *", "尺寸", "材质", "数量", "单价 *", "合计金额", "备注"]
KIND_LABELS = {"history": "历史账单", "recon": "核对账单"}
SCOPE_TEXT = "全部期间（按供应商分类）"
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')


def _safe(name):
    return _ILLEGAL.sub("_", str(name or "")).strip() or "未命名"


def _row_date(r):
    """回灌用日期：优先真实账单日期，缺失时用月份首日补齐（清单里会标注）。"""
    d = str(r.get("bill_date") or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        return d, False
    p = str(r.get("period") or "").strip()
    if re.match(r"^\d{4}-\d{2}$", p):
        return p + "-01", True
    return "", True


def plan(filters):
    """只统计数据、不落盘：按「(类型, 供应商)」分组，返回文件清单与分组明细。

    分组维度**只有供应商**——一家供应商一个分表，内容是该供应商的全部历史记录，
    不按月拆分（导出粒度就是「按供应商分类」）。
    """
    data = db.query_backup_rows(
        kinds=filters["kinds"], suppliers=filters["suppliers"],
        include_excluded=filters["include_excluded"],
    )
    groups = {}
    for kind in filters["kinds"]:
        for r in data.get(kind, []):
            key = (kind, r.get("supplier") or "未填供应商")
            groups.setdefault(key, []).append(r)

    files = []
    for (kind, supplier), rows in sorted(groups.items(), key=lambda x: (x[0][0], x[0][1])):
        dates, periods, filled = [], set(), 0
        for r in rows:
            d, was_filled = _row_date(r)
            if d:
                dates.append(d)
            if was_filled:
                filled += 1
            p = (r.get("period") or "")[:7]
            if p:
                periods.add(p)
        amount = 0.0
        for r in rows:
            try:
                if r.get("total_amount") not in (None, ""):
                    amount += float(r["total_amount"])
                elif r.get("quantity") not in (None, "") and r.get("unit_price") is not None:
                    amount += float(r["quantity"]) * float(r["unit_price"])
            except (TypeError, ValueError):
                pass
        ps = sorted(periods)
        files.append({
            "kind": kind,
            "kind_label": KIND_LABELS.get(kind, kind),
            "supplier": supplier,
            "period_count": len(ps),
            "period_from": ps[0] if ps else "",
            "period_to": ps[-1] if ps else "",
            "period_range": ("%s ~ %s" % (ps[0], ps[-1])) if ps else "",
            "rows": len(rows),
            "date_from": min(dates) if dates else "",
            "date_to": max(dates) if dates else "",
            "amount": round(amount, 2),
            "date_filled": filled,
            "name": "%s_%s_全部记录.xlsx" % (KIND_LABELS.get(kind, kind), _safe(supplier)),
        })
    return files, groups


def _write_sheet(path, rows):
    """写单个标准模板分表（表头 9 列，日期缺失按月份首日补齐）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "账单数据"
    head_fill = PatternFill("solid", fgColor="FF4472C4")
    for j, h in enumerate(BACKUP_HEADERS, 1):
        c = ws.cell(row=1, column=j, value=h)
        c.font = Font(bold=True, color="FFFFFFFF")
        c.fill = head_fill
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for i, r in enumerate(rows, 2):
        date, _filled = _row_date(r)
        vals = [r.get("supplier"), date, r.get("model"), r.get("size"), r.get("material"),
                r.get("quantity"), r.get("unit_price"), r.get("total_amount"), r.get("remark") or ""]
        for j, v in enumerate(vals, 1):
            c = ws.cell(row=i, column=j, value=v)
            if j in (6, 7, 8) and isinstance(v, (int, float)):
                c.number_format = "0.00" if j != 6 else "0.##"
    for j, w in enumerate([14, 12, 22, 16, 14, 10, 11, 12, 24], 1):
        ws.column_dimensions[get_column_letter(j)].width = w
    ws.freeze_panes = "A2"
    wb.save(path)


def zip_name(filters, files=None):
    """ZIP 名称。有实际文件清单时按**真实内容**命名（避免勾了 3 家、实际只导出 1 家这种不符）。"""
    kind_tag = "历史" if filters["kinds"] == ["history"] else (
        "核对" if filters["kinds"] == ["recon"] else "历史+核对")
    if files:
        sups = sorted({f["supplier"] for f in files})
        selected_all = False
    else:
        sups = sorted(set(filters.get("suppliers") or []))
        selected_all = not filters.get("suppliers")
    if not sups:
        sup_tag = "全部供应商" if selected_all else "无数据"
    elif len(sups) == 1 and not selected_all:
        sup_tag = _safe(sups[0])
    elif selected_all:
        sup_tag = "全部供应商"
    else:
        sup_tag = "%d家供应商" % len(sups)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return "账单备份_%s_%s_全部期间_%s.zip" % (kind_tag, sup_tag, ts)


def build(filters):
    """生成备份 ZIP，返回 (zip 路径, 清单 dict)。"""
    files, groups = plan(filters)
    if not files:
        raise ValueError("当前条件下没有可导出的账单数据，请检查供应商勾选与账单类型")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = zip_name(filters, files)
    path = os.path.join(OUTPUT_DIR, name)
    tmpdir = os.path.join(OUTPUT_DIR, "_backup_tmp_%s" % ts)
    os.makedirs(tmpdir, exist_ok=True)

    manifest_files = []
    try:
        for f in files:
            key = (f["kind"], f["supplier"])
            rows = groups[key]
            fp = os.path.join(tmpdir, f["name"])
            _write_sheet(fp, rows)
            with open(fp, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()[:16]
            f["sha256"] = digest
            f["size"] = os.path.getsize(fp)
            manifest_files.append(dict(f))

        total_rows = sum(f["rows"] for f in manifest_files)
        manifest = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "filters": {
                "kinds": filters["kinds"],
                "kinds_text": "、".join(KIND_LABELS.get(k, k) for k in filters["kinds"]),
                "suppliers": filters.get("suppliers") or ["全部"],
                "scope": SCOPE_TEXT,
                "include_excluded": bool(filters.get("include_excluded")),
            },
            "totals": {
                "files": len(manifest_files),
                "rows": total_rows,
                "amount": round(sum(f["amount"] for f in manifest_files), 2),
                "history_rows": sum(f["rows"] for f in manifest_files if f["kind"] == "history"),
                "recon_rows": sum(f["rows"] for f in manifest_files if f["kind"] == "recon"),
            },
            "files": manifest_files,
            "restore": ("解压后从系统入口①「历史数据上传」按标准模板上传即可回灌；"
                        "相同「供应商+型号+单价+月份」的记录会自动跳过，重复导入安全。"
                        "日期以月份首日补齐的行已在清单 date_filled 中标注。"),
        }

        # 人读清单 CSV
        buf = StringIO()
        w = csv.writer(buf)
        w.writerow(["文件名", "账单类型", "供应商", "覆盖期间", "期间数", "行数", "最早日期", "最晚日期",
                    "金额合计", "日期补齐行数", "SHA256前16位"])
        for f in manifest_files:
            w.writerow([f["name"], f["kind_label"], f["supplier"], f["period_range"],
                        f["period_count"], f["rows"], f["date_from"], f["date_to"],
                        f["amount"], f["date_filled"], f["sha256"]])
        csv_text = buf.getvalue()

        restore_txt = (
            "账单备份 · 恢复说明\n"
            "==================================================\n"
            "生成时间：%s\n"
            "导出范围：账单类型=%s；供应商=%s；范围=%s\n"
            "文件数量：%d 个；记录总数：%d 条；金额合计：%.2f 元\n"
            "--------------------------------------------------\n"
            "说明：本包按供应商分类，一家供应商一个分表，内容为该供应商的全部历史记录\n"
            "      （不按月拆分、不支持按月导出）。\n"
            "--------------------------------------------------\n"
            "目录结构：\n"
            "  历史账单_<供应商>_全部记录.xlsx  按标准录入模板 9 列输出（供应商/日期/型号/尺寸/材质/数量/单价/合计金额/备注）\n"
            "  核对账单_<供应商>_全部记录.xlsx  同上；核对状态等派生信息不写入分表，改由 备份清单.json 记录\n"
            "  备份清单.csv / 备份清单.json     行数、覆盖期间、金额与 SHA256 校验值\n"
            "--------------------------------------------------\n"
            "如何恢复：\n"
            "  1) 解压本压缩包；\n"
            "  2) 打开系统「① 历史数据上传」，把需要恢复的 xlsx 拖入（可多选）；\n"
            "  3) 上传后系统照常审查并追加，相同「供应商+型号+单价+日期+数量+金额+备注」的记录自动跳过，\n"
            "     重复导入不会产生重复数据；\n"
            "  4) 上传成功后「历史数据上传」页会显示本次新增条数与累计条数，可与清单核对。\n"
            "--------------------------------------------------\n"
            "注意：\n"
            "  · 分表日期若为某月 1 日，表示原数据只有月份、无具体日期，已按月份首日补齐；\n"
            "  · 被排除的无效价（空价/待核价）默认不导出，如需一并备份请勾选「包含无效价行」；\n"
            "  · 核对账单回灌后成为历史价格基准，请确认业务口径后再导入。\n"
            % (manifest["generated_at"], manifest["filters"]["kinds_text"],
               "、".join(manifest["filters"]["suppliers"]), manifest["filters"]["scope"],
               manifest["totals"]["files"], manifest["totals"]["rows"], manifest["totals"]["amount"])
        )

        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in manifest_files:
                zf.write(os.path.join(tmpdir, f["name"]), f["name"])
            zf.writestr("备份清单.csv", csv_text)
            zf.writestr("备份清单.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            zf.writestr("恢复说明.txt", restore_txt)
    finally:
        for f in os.listdir(tmpdir):
            try:
                os.remove(os.path.join(tmpdir, f))
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass

    return path, manifest
