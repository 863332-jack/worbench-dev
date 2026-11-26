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

# 供应商识别关键词（文件名/表名）
SUPPLIER_KEYS = {
    "杰盛": "杰盛",
    "新新精密": "新新精密",
    "新新": "新新精密",
    "云锦": "云锦",
    "源宝": "云锦",
}

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
