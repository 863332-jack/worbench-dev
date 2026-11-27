# NAS 运维与优化方案（v1.1.0）

> 面向对象：日常维护这套系统的人。
> 前提：数据已迁移到 NAS（1774 条历史价 / 696 个型号基准），**本文所有操作都不触碰 `storage/`，
> 不需要再传一次数据库**。
> 本机（Mac）上的开发实例已停用，代码里仍保留的本地适配逻辑仅作为离线调试入口。

---

## 一、现状快照（2026-11-27 15:13 实测）

| 项目 | 实测值 |
|---|---|
| NAS | DS1621+（AMD Ryzen V1500B / x86_64），DSM 7.4.1-90080 Update 2 |
| Container Manager | 24.0.2-1707 |
| 项目 | `recon` → RUNNING，容器 `recon-system` |
| 部署路径 | **`/volume3/docker/recon`**（docker 共享在 volume3，不是 volume1） |
| 访问地址 | `http://192.168.1.10:5088`（首页 200 / 20ms，静态资源 200） |
| 数据 | `hist_price` 1774 条，`price_index` 696 条，`recon.db` 0.75 MB |
| 磁盘 | 总量 926.3 GB，可用 330.6 GB（已用 64.3%） |
| 内存 | 32 GB 总量，系统实际占用 6% |

---

## 二、仍依赖本地环境的配置 / 路径 / 脚本 —— 逐项处置

| # | 项目 | 位置 | 现状 | 处置 |
|---|---|---|---|---|
| 1 | 数据/上传/导出/日志目录 | `config.py` 的 `BASE_DIR/data|uploads|outputs|logs` | ✅ 已容器化：路径相对代码目录解析，容器内是 `/app/*`，由 compose 的 4 个 volume 挂到 `storage/*` | **不改**。换机器不用改代码 |
| 2 | 管理员密码默认值 | `config.py` `ADMIN_PASSWORD = env or "13662566518"` | ⚠️ 仍是"静默回退"：env 缺失时退回已泄露的旧密码 | **已改**：compose 用 `${RECON_ADMIN_PASSWORD:?…}`，缺变量直接失败而不是降级 |
| 3 | 局域网 IP 探测 | `app.py` `lan_ip()`（仅启动时打印） | ✅ 容器内已判 `/.dockerenv`，只打印提示 | **保留**。本机离线调试仍需要 |
| 4 | 本机双击启动脚本 | `启动服务.command` | 只在 Mac 上有意义 | **不迁移**（`.dockerignore` 已排除，不进镜像） |
| 5 | 冒烟测试 | `scripts/smoke_test.py` | ❌ 依赖本地 DB 文件路径，且会**清空数据库** | **不在 NAS 跑**。新增 `scripts/nas_verify.py`（纯 HTTP、默认只读）替代 |
| 6 | 部署文档中的路径 | `NAS-Docker部署指南.md` | ❌ 全文假设 `/volume1`，实际是 `/volume3` | **已加卷号提示**，避免"改了另一个卷的文件" |
| 7 | 本机 5088 实例 | 本机进程 | 已停 | **保持停止**。两边同时跑会各写一份 `recon.db`，容易搞混哪份是准的 |
| 8 | Python 依赖 | `requirements.txt` | ✅ 版本全钉死（Flask 3.1.3 / openpyxl 3.1.5 / requests 2.34.2 / xlrd 2.0.2） | **不改**。避免 NAS 构建时拉到不兼容的新版本 |
| 9 | 临时文件 | `exporter.py` / `backup.py` | ✅ 落在 `outputs/`、`data/backups/`，已是挂载目录；文件名带 uuid 后缀 | **不改** |
| 10 | `.env` 的读取方 | `docker-compose.yml`（不是 Python） | ⚠️ 只在**创建容器时**注入 | **记住**：改 `.env` 后必须"重新构建/重新启动项目"，`docker restart` 不会重读 env |

---

## 三、NAS 权限 / 存储 / 资源核查结果

### 3.1 文件属主（实测）

```
/volume3/docker/recon               owner=jack   755
/volume3/docker/recon/app           owner=jack   755
/volume3/docker/recon/.env          owner=jack   755   ← 注意
/volume3/docker/recon/storage/data/recon.db   owner=root
/volume3/docker/recon/storage/logs/app.log    owner=root
/volume3/docker/recon/storage/uploads/*       owner=root
```

**结论**：容器以 root 运行，写出来的文件（库、日志、上传件）都归 root。`jack` 是 administrators
组成员且共享是 ACL 模式，日常浏览/下载不受影响；但**手工替换 `recon.db` 时**要注意属主会变成 jack，
容器（root）仍可读写，不影响运行——只是别再叠加"改 user"的改动，否则会写不进去。

### 3.2 `.env` 权限（唯一实质风险）

`.env` 是 `755`，意味着任何能进入 `docker` 共享的用户都能读到管理员密码。
**建议改为 600**（File Station → 右键 `.env` → 属性 → 权限 → 去掉"其他"的读），
或在控制面板把 `docker` 共享的访问权限收紧到 administrators。

### 3.3 存储路径与容量

- `storage/` 四个子目录分别挂到容器 `/app/data`、`/app/uploads`、`/app/outputs`、`/app/logs`
- 当前库 0.75 MB；即使每月新增 1 万条历史价，10 年也在 100 MB 量级 —— 容量不是问题
- 磁盘剩 330 GB，导出 Excel 与备份 ZIP 都是 KB 级，**不需要额外清理策略**
- ⚠️ **不要把 `storage/` 放到 SMB/NFS 网络盘**：SQLite 的 WAL 依赖文件锁，网络文件系统会导致库损坏

### 3.4 资源占用与限制

- 实测容器常驻内存约 60–90 MB，峰值（并发解析 Excel）约 300 MB
- 本轮已在 compose 加了 `mem_limit: 1g` / `cpus: "2.0"`，给 NAS 上其他套件留出余量
- 日志双重限流：应用日志 `RotatingFileHandler` 1 MB×5；容器日志 `json-file` 10 MB×3

---

## 四、本轮已实施的代码改动（v1.1.0）

| 文件 | 改动 | 为什么 |
|---|---|---|
| `config.py` | 新增 `APP_VERSION`、`LOG_LEVEL`、`MAINTENANCE_ENABLED`、`MAINTENANCE_INTERVAL_HOURS`、`DAILY_BACKUP`；`RETENTION_DAYS`/`BACKUP_KEEP` 支持环境变量 | 运维参数不再需要改代码重建 |
| `app.py` | **后台维护线程**：每 N 小时清理超期文件 + 每日自动备份数据库 | 原来只在启动时清理，NAS 上容器几十天不重启 = 永远不清理；原来只有"清空历史库"才备份 = 生产库等于没有定期备份 |
| `app.py` | `/api/status` 增加 `version`、`uptime_sec`、`db_mb`、`wal_mb`、`output_files`、`writable` | 判断"跑的是不是新版本""数据卷是不是变只读了""WAL 有没有膨胀"，全都不需要登 NAS |
| `app.py` | 新增 **`GET /api/admin/diagnostics`**（仅管理员） | 没有 SSH 时唯一的容器内部视图：五个目录的存在性/可写性、SQLite 页数/空闲页/WAL、备份清单、磁盘余量、生效配置 |
| `app.py` | 启动横幅打印版本号、日志级别、保留策略 | 从 Container Manager 的日志就能确认参数是否生效 |
| `db.py` | 连接级 PRAGMA：`cache_size=16MB`、`temp_store=MEMORY`、`mmap_size=128MB`；`synchronous=NORMAL` 做成 `RECON_SQLITE_FAST` 开关（默认关） | 只提速不改持久化语义；`NORMAL` 有断电丢最后几笔的代价，因此不默认开启 |
| `docker-compose.yml` | 版本号、7 个运维环境变量、`stop_grace_period: 20s`、`mem_limit` / `cpus`、密码 `:?` 强制 | 见上 |
| `Dockerfile` | `ARG APP_VERSION` + OCI label + `ENV RECON_VERSION` | `docker inspect` 就能看出镜像版本 |
| `scripts/nas_verify.py` | 新增：纯 HTTP 体检，默认只读；`--full` 仅限空库 | 替代会清库的 `smoke_test.py`，可在生产上随时跑 |
| `NAS-Docker部署指南.md` | 卷号提示 | 防止改错卷 |

---

## 五、需要你确认的配置项

| # | 配置项 | 现取值 | 建议 | 影响 / 怎么改 |
|---|---|---|---|---|
| 1 | `RECON_ADMIN_PASSWORD` | `.env` 中已有 | **保持**，并把 `.env` 权限改 600 | 改密码：编辑 `.env` → 在 Container Manager 里**重新构建项目**（`restart` 不生效） |
| 2 | compose 的 `${VAR:?}` 语法 | 已启用 | 需确认 Container Manager 能解析 | 若"构建"时报 compose 语法错误，把该行改成 `RECON_ADMIN_PASSWORD: ${RECON_ADMIN_PASSWORD:-}` 即可（但会恢复"缺变量则回退旧密码"的行为） |
| 3 | `mem_limit: 1g` / `cpus: "2.0"` | 已启用 | 保持 | 若重建报未知字段，注释掉这两行，不影响启动 |
| 4 | `RECON_RETENTION_DAYS` | 90 | **按对账周期定**：能支撑季度复查 → 建议 180 | 改 `.env` 后重建 |
| 5 | `RECON_DAILY_BACKUP` | 1（开） | 保持开，`RECON_BACKUP_KEEP=10` | 备份落在 `storage/data/backups/`，约 0.75 MB/份 |
| 6 | `RECON_SQLITE_FAST` | 0 | 保持 0 | 只有导入大文件明显卡时才考虑改 1 |
| 6b | `RECON_LOW_QTY_THRESHOLD` | 20（件） | 先按 20 观察一轮 | 单价全等但历史订单量 < 该值时转「需人工复核」（疑打样价）。改 `.env` 后**重建项目**；设 0 关闭 |
| 7 | 异常处理后怎么通知人 | 无 | 建议加：DSM「通知」里把 Container Manager 的容器停止事件打开 | 控制面板 → 通知 → 规则，勾选 Docker/容器相关 |
| 8 | 是否对公网开放 | 仅局域网 | **不要开**（无 HTTPS、Flask 开发服务器、密码单因子） | 必须外网时走 VPN 或加 nginx + HTTPS |

---

## 六、发布到 NAS 的步骤（不碰数据）

> 关键：`storage/` 全程不动，`recon.db` 不会重新迁移。

1. **先在 NAS 上做一次手工备份**（可选但推荐）：
   File Station 把 `/volume3/docker/recon/storage/data/recon.db` 复制一份到别的目录。
2. 只上传这几个文件到 `/volume3/docker/recon/`：
   - `app/` 下的 `app.py`、`config.py`、`db.py`、`Dockerfile`（Dockerfile 同级）
   - 根目录的 `docker-compose.yml`（覆盖）
   - `app/scripts/` 不需要上传（`.dockerignore` 排除，只在本机用）
3. Container Manager →「项目」→ `recon` →「**构建**」（会重新 build 镜像并重建容器）。
4. 观察构建日志，等到 `Container recon-system Started`。
5. 立刻验证（见下节）。

> 顺序不要反：先传代码再构建。只构建不传代码，构建出来的还是旧代码。

---

## 七、验证方式

### 第 1 层：容器层（30 秒）

```bash
# 在能访问 NAS 的机器上
curl -s http://192.168.1.10:5088/api/status
```

期望：`"version":"1.1.0"`、`"hist_rows":1774`、`"idx_count":696`、`"writable":true`、`"db_mb":0.75`。

- `version` 仍是旧值/缺失 → 镜像没重建，回第 3 步
- `hist_rows` 不是 1774 → **立刻停手**，检查 compose 的 volumes 是否被改动

### 第 2 层：HTTP + 权限层（1 分钟）

```bash
cd recon_system
RECON_PW='你的管理员密码' python3 scripts/nas_verify.py
```

期望：`失败 0 项`。脚本会覆盖：健康字段、三个静态资源、未登录门禁 403、
旧密码被拒、诊断接口各路径可写、WAL 正常、磁盘余量、备份存在、维护开关已开。

### 第 3 层：业务层（只在新环境或空库执行）

```bash
RECON_PW='密码' python3 scripts/nas_verify.py --full
```

⚠️ **现在不要跑**：库里有 1774 条真实数据，脚本会直接拒绝（这是刻意设计）。
只有在将来搭了测试环境、库为空时才用它跑"上传→核对→下载→备份→还原"全流程。

生产环境的业务验证改为**一次人工冒烟**：用一份真实的待核对账单跑一次核对，确认
5 个状态统计合理、Excel 能下载、结果里"核对明细"行数与原表一致。

---

## 八、回滚

- 代码层：把 `app.py`/`config.py`/`db.py` 换回上一版，重新构建即可。数据库结构未变（**本轮没有改表结构**），recon.db 无需回滚。
- compose 层：本次只增加了 env 与资源限制，回滚就是删掉对应行再构建一次。
- 数据层：`storage/data/backups/recon_auto_*.db` 是每日快照，恢复时**先停容器**，再把备份文件复制成 `recon.db`（同时删掉 `recon.db-wal` / `recon.db-shm`），再启动。

---

## 九、后续可选项（现在不做，先记录）

| 优化 | 收益 | 为什么不现在做 |
|---|---|---|
| 换 gunicorn（1 worker + 8 threads） | 去掉 "development server" 警告，连接管理与超时更规范 | 要加依赖、改 CMD；SQLite 只能单写，多进程反而会锁冲突。收益小、变更风险不小 |
| 容器改非 root 运行 | 更安全 | `storage/` 现有文件属主是 root，必须先 `chown -R`，需要 SSH（当前 SSH 打不开） |
| nginx 反代 + HTTPS | 可对公网、可加访问日志 | 当前只在内网用，没有需求 |
| 备份异地同步（Hyper Backup 抓 `storage/data/backups/`） | 防 NAS 整机故障 | 需要你在 DSM 里配一次任务，属于策略决定 |
| 数据库归档（历史价按年分表） | 库更小 | 1774 条离需要分表还很远 |

---

## 十、明确不做的事

- ❌ **不重新迁移数据**：`storage/` 不动，`recon.db` 保持 1774 条
- ❌ **不改表结构**：本轮无 schema 变更，因此不需要任何数据迁移脚本
- ❌ **不改端口**：仍是 `5088:5088`
- ❌ **不动 `app/` 下的重复 `docker-compose.yml`**（`.dockerignore` 已把它排除在镜像外，仅目录里多一份；建议你在 File Station 里删掉它，避免以后误在 `app/` 里执行 compose）
