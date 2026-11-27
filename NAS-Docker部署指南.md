# 账单核对系统 · 群晖 NAS Docker 部署指南

目标：在群晖 DSM 7.x（x86_64）上用 Container Manager 跑起来，局域网内设备通过
`http://NAS的IP:5088` 访问，数据落在 NAS 本地磁盘，容器重建不丢。

---

## 一、部署后的目录结构

> **路径以实际卷号为准**：下文统一写 `/volume1/docker/recon`，但 `docker` 共享文件夹
> 落在哪个存储卷取决于建共享时的选择。**本机实测在 `/volume3`**，即
> `/volume3/docker/recon`。在 File Station 里右键 `docker` 共享 →「属性」可以看到
> 「位置」。看错卷号是排查"改了文件却没生效"的第一大原因——容器挂的是另一个卷。

在 NAS 上最终长这样（示例按 `/volume1` 写，实际请替换为你的卷号）：

```
/volume1/docker/recon/
├── docker-compose.yml        编排文件
├── .env                      管理员密码（chmod 600，不进 git）
├── app/                      项目源码 + Dockerfile
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app.py / db.py / config.py ...
│   ├── templates/
│   └── static/
└── storage/                  ← 持久化数据，容器删了也在
    ├── data/                 recon.db + backups/
    ├── uploads/              上传的历史账单、待核对账单
    ├── outputs/              核对结果 Excel、备份 ZIP
    └── logs/                 app.log
```

四个 `storage` 子目录分别对应容器内的 `/app/data`、`/app/uploads`、`/app/outputs`、`/app/logs`。

---

## 二、部署前必须确认的三件事

### 1. NAS 能拉取 Docker Hub 镜像

在 Container Manager → 注册表 里搜 `python`，能搜到就是通的。国内网络经常拉不动，先在
**Container Manager → 注册表 → 设置 → 新增镜像仓库** 加一个加速地址（如阿里云容器镜像服务的
专属加速器），否则构建到 `FROM python:3.13-slim` 这一步就会卡死。

### 2. 5088 端口没被占用

DSM 自身占 5000/5001/5005/5006/6690。可在 **控制面板 → 登录门户** 里核对，或在 SSH 里执行：

```bash
sudo netstat -tlnp | grep 5088
```

无输出即未占用。

### 3. 存储空间用的是本地卷

⚠️ **`storage/` 必须放在 NAS 本地磁盘（Btrfs / ext4）**，绝对不能放在会被 SMB/NFS 共享出去
的路径上再让别的机器挂载。SQLite 的 WAL 模式依赖本地文件锁，网络文件系统上会出现
`database is locked` 甚至库损坏。

---

## 三、部署步骤

### 步骤 1：在 NAS 上建目录

SSH 登录 NAS（控制面板 → 终端机和 SNMP → 启用 SSH），执行：

```bash
sudo mkdir -p /volume1/docker/recon/{app,storage/data,storage/uploads,storage/outputs,storage/logs}
```

不想开 SSH 的话，用 File Station 在 `docker` 共享文件夹下手工建同名目录也可以。

### 步骤 2：上传代码

把 Mac 上 `recon_system/` 里的这些内容传到 NAS 的 `/volume1/docker/recon/app/`：

- 全部 `.py` 文件（`app.py`、`db.py`、`config.py`、`backup.py`、`exporter.py`、`field_map.py`、
  `hist_db.py`、`matcher.py`、`parsers.py`、`recon_stats.py`、`validator.py`）
- `templates/`、`static/` 两个目录
- `Dockerfile`、`requirements.txt`

**不要传**：`data/`、`uploads/`、`outputs/`、`logs/`、`__pycache__/`、`scripts/`、`tests/`、
`启动服务.command`（这些已被 `.dockerignore` 排除，传了也会被忽略，但传大数据会白等）。

再传 `docker-compose.yml` 到 `/volume1/docker/recon/`（注意是上一级）。

Mac 上用命令行最省事：

```bash
cd /Users/jack/WorkBuddy/spaces/2026_11_26_10-15-55
rsync -av --exclude 'data' --exclude 'uploads' --exclude 'outputs' --exclude 'logs' \
      --exclude '__pycache__' --exclude 'scripts' --exclude 'tests' --exclude '.git' \
      recon_system/ 你的NAS用户名@NAS的IP:/volume1/docker/recon/app/
```

### 步骤 3：设置管理员密码

```bash
cd /volume1/docker/recon
sudo cp app/.env.example .env
sudo nano .env          # 把 RECON_ADMIN_PASSWORD 改成你自己的强密码
sudo chmod 600 .env
```

> **为什么必须改**：代码里原来内置的密码 `13662566518` 已经随公开 GitHub 仓库
> `worbench-dev` 暴露了。部署到 NAS 后局域网里更多人能访问这个服务，不改等于把
> 「清空数据库」的权限公开。

`.env` 内容形如：

```ini
RECON_ADMIN_PASSWORD=你的强密码
```

### 步骤 4：用 Container Manager 启动项目

**图形界面方式（推荐）**：

1. Container Manager → **项目** → **新增**
2. 项目名称填 `recon`
3. 路径选 `/volume1/docker/recon`
4. 来源选 **使用现有的 docker-compose.yml**
5. 点「下一步」→「完成」，它会自动构建镜像并启动

首次构建约 2–5 分钟（装依赖为主），在「项目 → recon → 容器 → 日志」里能看到实时输出。

**SSH 方式**（等价，排障更方便）：

```bash
cd /volume1/docker/recon
sudo docker compose up -d --build
sudo docker compose ps
```

---

## 四、容器运行配置说明

对应 `docker-compose.yml` 的关键项，逐条说明为什么这么设：

| 配置 | 值 | 说明 |
|------|-----|------|
| `ports` | `5088:5088` | 左边是 NAS 端口、右边是容器端口。想换成 8080，就改成 `"8080:5088"`，容器内代码不用动 |
| `restart` | `unless-stopped` | NAS 重启、容器崩溃会自动拉起；你手动停止的不会被强制唤起 |
| `TZ` | `Asia/Shanghai` | 不设的话容器是 UTC，账单月份判断和「保留 90 天」清理会差 8 小时 |
| `RECON_ADMIN_PASSWORD` | 从 `.env` 读 | 覆盖代码内置密码，`.env` 权限 600 |
| `volumes` | 4 条 | 数据库/上传件/导出件/日志分别挂出来，容器升级重建都不丢 |
| `logging` | `max-size 10m` / `max-file 3` | 不限制的话 NAS 磁盘会被容器日志写满 |
| `healthcheck` | `/api/status` | 同时检查数据库可读性与索引，比 TCP 探测有意义 |
| **单副本** | 不要 scale | SQLite WAL 模式，多容器挂同一个库必然锁冲突 |

**资源占用参考**：空闲时约 80–120 MB 内存，核对 5000 行账单的峰值约 300 MB。DSM 上无需
特别限制；若 NAS 内存紧张（4 GB 以下），可在 compose 里加：

```yaml
    deploy:
      resources:
        limits:
          memory: 512M
```

**关于 root 运行**：镜像默认 root，因为群晖 `/volume1/docker` 目录属主不固定，固定非 root
用户极易写不进挂载目录而启动失败。代价是生成的文件属主是 root，用 File Station 删不掉。
要改非 root：先在 NAS 执行 `chown -R 1026:100 /volume1/docker/recon/storage`（1026:100 是
群晖 admin 的 UID:GID，用 `id admin` 确认），再取消 compose 里 `user: "1026:100"` 的注释。

---

## 五、局域网访问地址与网络设置

部署完成后访问：

```
http://NAS的IP:5088
```

比如 NAS 是 `192.168.8.10`，就是 `http://192.168.8.10:5088`。

查 NAS IP：DSM → 控制面板 → 信息中心 → 网络，或 SSH 执行 `ip a | grep 192.168`。

**其他设备打不开时按顺序排查**：

1. **DSM 防火墙**：控制面板 → 安全性 → 防火墙，确认放行 5088 端口（TCP）
2. **路由器 AP 隔离**：部分路由开启了「AP 隔离/客户端隔离」，同 WiFi 设备互不可见，需关闭
3. **访客网络**：连的是 Guest WiFi 通常无法访问内网主机
4. **端口是否真的映射了**：NAS 上执行 `curl -I http://127.0.0.1:5088` 应返回 200；
   如果本机能通、别的设备不通，问题一定在防火墙或网络隔离，不在容器

**不要用 `network_mode: host`**：会绕过 DSM 端口管理，且与 5088 冲突时难以排查，桥接映射足够。

**想要 HTTPS / 域名访问**：用 DSM 自带的 **控制面板 → 登录门户 → 高级 → 反向代理**，
来源 `https://recon.你的域名`，目标 `http://127.0.0.1:5088`，再套一个 Let's Encrypt 证书即可。
容器侧不用改任何配置。

---

## 六、把现有数据迁到 NAS（可选）

Mac 上现在有 1774 条历史价、1 个核对批次。想一起搬过去：

```bash
# 1. 先停掉 Mac 上的服务，避免拷贝时库正在写
lsof -ti :5088 | xargs kill

# 2. 拷数据库（含 backups 子目录）
rsync -av /Users/jack/WorkBuddy/spaces/2026_11_26_10-15-55/recon_system/data/ \
      你的NAS用户名@NAS的IP:/volume1/docker/recon/storage/data/

# 3. 拷上传原件与导出件（可选，数据量大可跳过）
rsync -av /Users/jack/WorkBuddy/spaces/2026_11_26_10-15-55/recon_system/uploads/ \
      你的NAS用户名@NAS的IP:/volume1/docker/recon/storage/uploads/
```

拷完在 NAS 上修正属主，然后重启容器：

```bash
sudo chown -R 1026:100 /volume1/docker/recon/storage
cd /volume1/docker/recon && sudo docker compose restart
```

**注意**：SQLite 库必须**停服后**拷贝（WAL 模式下 `recon.db-wal` 里可能还有未合并的事务）。
如果只想搬数据不要残留风险，更稳的做法是在 Mac 上用页面的「账单备份导出」生成 ZIP，
到 NAS 部署好之后重新导入。

---

## 七、部署后验证要点

按顺序过一遍，全绿就算部署成功。

### 1. 容器状态

```bash
cd /volume1/docker/recon
sudo docker compose ps
```

应看到 `State: running`，且 `Health` 列在 30–60 秒后变成 `healthy`。

### 2. 容器内自检

```bash
sudo docker compose exec recon curl -s http://127.0.0.1:5088/api/status
```

没有 curl 就用：

```bash
sudo docker compose exec recon python3 -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:5088/api/status').read().decode())"
```

返回里应有 `"ok": true` 以及数据条数。

### 3. 三个关键地址（最容易出问题的地方）

在**另一台电脑**的浏览器依次打开：

| 地址 | 期望 |
|------|------|
| `http://NAS的IP:5088/` | 页面完整、有样式、右上角有「管理员登录」 |
| `http://NAS的IP:5088/static/style.css` | 直接显示一堆 CSS 代码（不是 500 报错页） |
| `http://NAS的IP:5088/static/app.js` | 直接显示 JavaScript 代码 |

> 这三条是我上一轮排查「页面打开异常」时总结出来的：如果首页能开、但后两个返回 500，
> 说明静态资源读取出了问题。容器环境下最常见原因是权限（挂载目录不可读）。

### 4. 管理员权限链路

1. 点右上角「管理员登录」，输入 `.env` 里设的新密码 → 应提示登录成功
2. 登录后页面自动重载，应出现「已导入批次」「供应商历史有效数据」「账单备份导出」三块
3. 用**旧密码** `13662566518` 登录 → 应提示密码错误（证明环境变量确实生效了）

### 5. 业务全流程跑一遍

用一份小的测试账单：

1. 上传历史数据 → 提示导入成功、条数正确
2. 上传账单 → 生成核对批次、状态统计有数字
3. 结果明细能翻页（每页 50 行）
4. 「下载结果 Excel」→ 文件能打开、含 6 个工作表
5. 「账单备份导出」→ 得到 ZIP，内含按供应商分的 xlsx

### 6. 数据持久化（最关键的一条）

```bash
cd /volume1/docker/recon
sudo docker compose down          # 删容器，不删卷
sudo docker compose up -d
```

重启后再看：**数据还在、管理员密码还是新的**。如果数据没了，说明卷挂载路径写错了，
立刻检查 `docker compose config` 里的 volumes 是否指向 `/volume1/docker/recon/storage/*`。

### 7. 中文与特殊字符

上传一个**中文文件名**的 Excel（如 `云锦-2026年3月账单.xlsx`），确认：
- 能正常上传不报错
- 在「已导入批次」里文件名显示正常不乱码
- 导出的 Excel/ZIP 文件名也是正常中文

> 容器里已设 `LANG=C.UTF-8`。若这里出问题，多半是 NAS 上文件名编码问题而非容器问题。

### 8. 日志落盘

```bash
tail -20 /volume1/docker/recon/storage/logs/app.log
```

能看到刚才那些请求的记录（时间应是北京时间），且 `storage/logs/` 下文件在增长——
说明日志卷挂载生效，不是写在容器内部。

---

## 八、常见问题

| 现象 | 原因 | 处理 |
|------|------|------|
| 构建卡在 `FROM python:3.13-slim` | 拉不到 Docker Hub | 配镜像加速器；或改用「本机构建导出 tar」方式 |
| 启动即退出，日志 `Permission denied` | 挂载目录不可写 | `sudo chown -R 1026:100 /volume1/docker/recon/storage`，或保持 root 运行 |
| 502 / 连接被拒 | 端口没映射或容器没起来 | `docker compose ps` 看状态；`docker compose logs --tail 50` 看原因 |
| 本机能访问、别的设备不能 | 防火墙或 AP 隔离 | 放行 5088；关闭路由器 AP 隔离 |
| `database is locked` | 多个容器/进程同时写库 | 确认只有一个 `recon-system` 容器：`docker ps` |
| 时间差 8 小时 | TZ 没生效 | 确认 compose 里 `TZ: Asia/Shanghai`，`docker compose exec recon date` 核对 |
| 页面无样式、按钮无反应 | 静态资源 500 | 看日志是否 `Permission denied`；确认 `static/` 已随代码上传到 `app/static/` |
| 改了代码不生效 | 没重新构建 | `docker compose up -d --build`（只 restart 用的是旧镜像） |

---

## 九、日常运维

```bash
cd /volume1/docker/recon

# 查看状态
sudo docker compose ps

# 看日志（容器标准输出）
sudo docker compose logs -f --tail 100

# 更新代码后重新构建
sudo docker compose up -d --build

# 备份数据（建议做成计划任务，每周一次）
sudo tar czf /volume1/docker/recon-backup-$(date +%Y%m%d).tar.gz storage/data

# 停止
sudo docker compose stop

# 彻底删除（数据仍在 storage/，不删目录就丢不了）
sudo docker compose down
```

建议在 DSM **控制面板 → 任务计划** 里加一条每周日的脚本任务，把 `storage/data` 打包到另一个
共享文件夹或外接硬盘——SQLite 单文件，备份成本很低，但没有异地副本时一块盘坏了就全没了。
