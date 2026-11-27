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
# 管理员密码：清空历史库 / 历史记录删除 / 账单导出备份共用。
# 优先读环境变量 RECON_ADMIN_PASSWORD —— 容器部署必须用这个方式注入，
# 不要把真实密码写进代码或 docker-compose.yml。
# 下面的内置值只是本机开发的兜底，且已于公开仓库暴露，生产环境务必覆盖。
ADMIN_PASSWORD = os.environ.get("RECON_ADMIN_PASSWORD") or "13662566518"
# 是否成功从环境变量取到密码（启动时据此提示风险）
ADMIN_PASSWORD_FROM_ENV = bool(os.environ.get("RECON_ADMIN_PASSWORD"))
# 管理员登录失败锁定：连续失败 N 次后锁定 M 秒
LOGIN_LOCK_AFTER = 5
LOGIN_LOCK_SECONDS = 600

# ---------- 运维配置（全部可用环境变量覆盖，便于容器部署时调节而不改代码） ----------
def _env_flag(name, default="1"):
    """把环境变量解析成布尔：0/false/no/off/空 视为关闭。"""
    v = os.environ.get(name, default)
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


def _env_int(name, default):
    try:
        return int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return int(default)


# 版本号：会出现在 /api/status 与启动横幅里。
# 用途：改完代码重建镜像后，用它确认 NAS 上跑的确实是新版本（避免"以为改了其实没生效"）。
APP_VERSION = os.environ.get("RECON_VERSION", "1.5.0")

# 上传原件/导出结果保留天数（启动时 + 每日维护时清理）
UPLOAD_RETENTION_DAYS = _env_int("RECON_RETENTION_DAYS", 90)
# 数据库自动备份保留份数
BACKUP_KEEP = _env_int("RECON_BACKUP_KEEP", 10)

# 后台维护线程：容器会连续运行数月不重启，只在启动时清理会导致超期文件永久堆积。
MAINTENANCE_ENABLED = _env_flag("RECON_MAINTENANCE", "1")
MAINTENANCE_INTERVAL_HOURS = float(os.environ.get("RECON_MAINTENANCE_HOURS", "24") or 24)
# 每日自动备份数据库（与清空前的备份共用 data/backups/ 目录，按 BACKUP_KEEP 轮转）
DAILY_BACKUP = _env_flag("RECON_DAILY_BACKUP", "1")

# 日志级别：常规 INFO；排障时设 RECON_LOG_LEVEL=DEBUG 并重启容器
LOG_LEVEL = os.environ.get("RECON_LOG_LEVEL", "INFO").upper()

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
DIFF_LOW_QTY = "low_qty"            # 全等匹配但历史订单数量过少（疑打样价/小批量价，非批量做货价）

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
    DIFF_LOW_QTY: "历史数量偏少",
}
# 需要人工处理的差异类型（用于「异常项」汇总与筛选）
DIFF_ATTENTION = (DIFF_AMT_HIGH, DIFF_AMT_LOW, DIFF_NO_HIST, DIFF_PARSE_ERR,
                  DIFF_MULTI_LEVEL, DIFF_DATE_BEFORE, DIFF_BASE_STALE,
                  DIFF_DATE_MISSING, DIFF_NEAR_MODEL, DIFF_LOW_QTY)

# 基准过期阈值（月）：历史最低价距本单账单月超过该月数，提示价格可能失效
BASE_STALE_MONTHS = 6
# 低数量复核阈值（件）：单价与历史最低价完全一致，但该价位的历史订单数量最多仍小于该值时，
# 不判定为「已匹配最低价」，转「需人工复核」。
# 原因：数量过少的订单很可能按打样价/小批量价成交，单价天然高于批量做货价；
# 反过来，若一个「低价」只出现在极小批量的订单上，它并不能证明批量采购能拿到该价格。
# 判定取「等于最低价的那些历史记录」中的最大数量：只要其中有一笔达到阈值，即认为该低价经过批量验证。
# 可用环境变量 RECON_LOW_QTY_THRESHOLD 覆盖；设 0 可关闭该规则。
LOW_QTY_THRESHOLD = _env_int("RECON_LOW_QTY_THRESHOLD", 20)
# 差异率展示精度（百分比小数位）
GAP_RATE_DIGITS = 2
