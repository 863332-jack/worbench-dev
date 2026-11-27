# 账单核对系统 · 生产镜像
# 目标平台：群晖 DSM 7.x (x86_64) Container Manager / 任意 docker compose 环境
#
# 设计要点：
#   1. 基础镜像用 slim，装 tzdata 保证容器内时间是北京时间，
#      否则账单月份判断、文件保留期清理会用 UTC，差 8 小时。
#   2. 不装 curl：健康检查用 python 自带 urllib，减少攻击面与体积。
#   3. 默认以 root 运行 —— 群晖上 /volume1/docker 下的目录属主不固定，
#      固定非 root 用户极易因写不进挂载目录而启动失败。
#      需要非 root 时见 docker-compose.yml 里 user: 的说明。

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

WORKDIR /app

# 时区数据（slim 镜像默认不带 /usr/share/zoneinfo）
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
 && echo $TZ > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

# 先装依赖再拷代码，改代码时可复用依赖层，重建快很多
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

# 数据目录（实际由 compose 的 volume 覆盖，这里只是保证目录存在）
RUN mkdir -p /app/data /app/uploads /app/outputs /app/logs

EXPOSE 5088

# 用 /api/status 判活：它同时检查数据库可读与索引状态，比单纯 TCP 探测有意义
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python3 -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5088/api/status', timeout=3).status==200 else 1)"

CMD ["python3", "app.py"]
