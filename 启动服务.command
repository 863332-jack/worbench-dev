#!/bin/bash
# ============================================================
# 账单核对系统 · 一键启动（macOS 双击运行）
#
# 作用：在系统终端里独立启动服务，不依赖 WorkBuddy / AI 会话环境。
# 关闭这个终端窗口 = 停止服务。
# ============================================================

cd "$(dirname "$0")" || exit 1

# 【关键】清掉 WorkBuddy 沙箱注入的 Python 钩子。
# 不清的话，服务进程的文件读取会被沙箱代理接管，代理一旦失效就会出现
# 首页能打开、但 style.css / app.js 全部 500（BrokerProtocolError），
# 页面变成无样式、按钮点了没反应。
unset PYTHONPATH
unset CODEBUDDY_SANDBOX_BROKER_CAPABILITY
unset CODEBUDDY_SANDBOX_BROKER_IPC_ADDRESS
unset NODE_OPTIONS

PORT=5088

# --- 选一个装了 flask / openpyxl 的解释器 ---
PY=""
for c in \
  "/Users/jack/.workbuddy/binaries/python/versions/3.13.12/bin/python3" \
  "$(command -v python3)" \
  "/usr/bin/python3"; do
  if [ -n "$c" ] && [ -x "$c" ] && "$c" -c "import flask, openpyxl" >/dev/null 2>&1; then
    PY="$c"
    break
  fi
done

if [ -z "$PY" ]; then
  echo "✗ 找不到安装了 flask / openpyxl 的 Python 3"
  echo
  echo "  请先安装依赖：python3 -m pip install flask openpyxl"
  echo
  read -n 1 -s -r -p "按任意键关闭窗口..."
  exit 1
fi

# --- 端口占用检查 ---
if lsof -ti :"$PORT" >/dev/null 2>&1; then
  echo "⚠ 端口 $PORT 已被占用，服务应该已经在运行了。"
  echo
  echo "  本机访问:   http://127.0.0.1:$PORT"
  echo "  局域网访问: http://$(ipconfig getifaddr en0 2>/dev/null || echo '未联网'):$PORT"
  echo
  echo "  如果页面异常、想重启服务，请先执行："
  echo "      lsof -ti :$PORT | xargs kill"
  echo
  read -n 1 -s -r -p "按任意键关闭窗口（不会停止正在运行的服务）..."
  exit 0
fi

echo "使用解释器: $PY"
echo

"$PY" app.py

echo
echo "服务已停止。"
read -n 1 -s -r -p "按任意键关闭窗口..."
