# -*- coding: utf-8 -*-
"""系统配置：路径、常量、状态枚举（集中管理，含管理员密码等敏感配置）。"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
LOG_DIR = os.path.join(BASE_DIR, "logs")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
DB_PATH = os.path.join(DATA_DIR, "recon.db")

for d in (DATA_DIR, UPLOAD_DIR, OUTPUT_DIR, LOG_DIR, BACKUP_DIR):
    os.makedirs(d, exist_ok=True)

# ---------- 安全配置（集中管理，避免多处硬编码漂移） ----------
# 管理员密码：清空历史库 / 历史记录删除共用（原 app.py 与 hist_db.py 两处硬编码，已收敛至此）
ADMIN_PASSWORD = "13662566518"
# 管理员登录失败锁定：连续失败 N 次后锁定 M 秒
LOGIN_LOCK_AFTER = 5
LOGIN_LOCK_SECONDS = 600

# ---------- 运维配置 ----------
# 上传原件/导出结果保留天数（启动时清理超期文件）
UPLOAD_RETENTION_DAYS = 90
# 数据库自动备份保留份数
BACKUP_KEEP = 10

# 供应商初始种子 (名称, 别名)：仅首次建库/升级时写入 supplier 表。
# 之后供应商一律以数据库为准，可在页面「供应商管理」中新增；这里不再作为校验白名单。
SEED_SUPPLIERS = [
    ("杰盛", ""),
    ("新新精密", "新新"),
    ("云锦", "源宝"),
]

# 匹配状态
STATUS_MATCHED_LOWEST = "matched_lowest"      # 已匹配最低价
STATUS_NEEDS_REVIEW = "needs_review"          # 需人工复核
STATUS_ABOVE_LOWEST = "above_lowest"          # 高于历史最低价（可核对）
STATUS_NO_HISTORY = "no_history"              # 无历史记录
STATUS_PARSE_ERROR = "parse_error"            # 解析失败

STATUS_LABELS = {
    STATUS_MATCHED_LOWEST: "已匹配最低价",
    STATUS_NEEDS_REVIEW: "需人工复核",
    STATUS_ABOVE_LOWEST: "高于历史最低价",
    STATUS_NO_HISTORY: "无历史记录",
    STATUS_PARSE_ERROR: "解析失败",
}

# 单价校验：排除待核价、空价、0价
EXCLUDE_MARKERS = ("待核", "待确认", "待定", "报价", "不详")

# ---------- 智能对账：差异识别口径 ----------
# 单价差异 = 本单单价 − 历史最低价（正数=超付，负数=低于基准）
DIFF_AMT_HIGH = "amt_high"          # 金额偏高：本单单价高于历史最低价
DIFF_AMT_LOW = "amt_low"            # 金额偏低：本单低于历史最低价（新高价/录入异常）
DIFF_NO_HIST = "no_hist"            # 无历史基准
DIFF_PARSE_ERR = "parse_err"        # 单价无法解析
DIFF_MULTI_LEVEL = "multi_level"    # 同型号多档价，最低价是否适用待确认
DIFF_DATE_BEFORE = "date_before"    # 账单月份早于基准月份（用后来的价格比对，逻辑可疑）
DIFF_BASE_STALE = "base_stale"      # 基准过期：历史最低价距账单月太久，价格可能已失效
DIFF_DATE_MISSING = "date_missing"  # 月份缺失，无法判断时效
DIFF_NEAR_MODEL = "near_model"      # 型号近似匹配（忽略符号差异后命中，需确认是否同一物料）

DIFF_LABELS = {
    DIFF_AMT_HIGH: "金额偏高",
    DIFF_AMT_LOW: "金额偏低",
    DIFF_NO_HIST: "无历史基准",
    DIFF_PARSE_ERR: "单价无法解析",
    DIFF_MULTI_LEVEL: "多档价待定",
    DIFF_DATE_BEFORE: "日期早于基准",
    DIFF_BASE_STALE: "基准过期",
    DIFF_DATE_MISSING: "月份缺失",
    DIFF_NEAR_MODEL: "型号近似匹配",
}
# 需要人工处理的差异类型（用于「异常项」汇总与筛选）
DIFF_ATTENTION = (DIFF_AMT_HIGH, DIFF_AMT_LOW, DIFF_NO_HIST, DIFF_PARSE_ERR,
                  DIFF_MULTI_LEVEL, DIFF_DATE_BEFORE, DIFF_BASE_STALE,
                  DIFF_DATE_MISSING, DIFF_NEAR_MODEL)

# 基准过期阈值（月）：历史最低价距本单账单月超过该月数，提示价格可能失效
BASE_STALE_MONTHS = 6
# 差异率展示精度（百分比小数位）
GAP_RATE_DIGITS = 2
