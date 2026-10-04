#!/bin/bash
# 灵桥安装脚本：在这台 Mac 上准备好灵桥的运行环境。可以重复运行；不会动你的会话数据。
# 用法：在灵桥文件夹里运行  bash install.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "== 安装灵桥 =="
if [ "$(uname -s)" != "Darwin" ]; then
  echo "灵桥只支持 macOS。"; exit 1
fi
ARCH="$(uname -m)"
if [ "$ARCH" != "arm64" ]; then
  echo "提示：这台 Mac 不是 Apple 芯片（$ARCH）。灵桥只在 Apple 芯片上测过，可以继续试，但不保证能用。"
fi

# 1. 找 Python 3.10 或更新的版本（推荐 Homebrew 的 Python 3.13）
PY=""
for CANDIDATE in "${BRIDGE_PYTHON:-}" /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3 /usr/local/bin/python3.13 /usr/local/bin/python3 "$(command -v python3 || true)"; do
  [ -n "$CANDIDATE" ] && [ -x "$CANDIDATE" ] || continue
  if "$CANDIDATE" -c 'import ssl, sys, venv; assert sys.version_info >= (3, 10)' >/dev/null 2>&1; then
    PY="$CANDIDATE"; break
  fi
done
if [ -z "$PY" ]; then
  cat <<'MSG'
没找到 Python 3.10 或更新的版本。请先装好再运行一次 bash install.sh：
  1. 装 Homebrew：打开 https://brew.sh ，把页面上那一行命令粘到终端里运行；
  2. 终端里运行：brew install python@3.13
MSG
  exit 1
fi
echo "用这个 Python：$PY"

# 2. 灵桥自己的运行环境（.bridge/runtime，只给灵桥用，不影响别的程序）
BRIDGE_PYTHON="$PY" /bin/bash app/repair-runtime.sh

# 3. 从这台 Mac 上装好的 Claude、Codex、ZCode、WorkBuddy 等 App 里提取图标；没装的显示字母徽标
echo "提取工具图标："
.bridge/runtime/bin/python3 app/brand_icons.py || echo "图标提取没成功，不影响使用。"

# 4. 从网页下载 ZIP 解压的，macOS 会给文件打"来自网络"的标记，双击 App 时会被拦下。只去掉灵桥这个 App 的标记。
#    经网页上传/下载的文件会丢掉"可执行"权限，这里给启动器和脚本补上。
chmod 755 "会话桥.app/Contents/MacOS/会话桥" app/repair-runtime.sh install.sh 2>/dev/null || true
xattr -dr com.apple.quarantine "会话桥.app" 2>/dev/null || true

echo
echo "装好了。双击「会话桥.app」打开灵桥，可以把它拖到程序坞。"
echo "以后更新：灵桥侧栏底部点「检查更新」；或者在这个文件夹里运行 git pull，再重新打开灵桥。"
