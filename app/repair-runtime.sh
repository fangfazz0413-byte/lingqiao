#!/bin/bash
# Restore Lingqiao's dedicated desktop environment using an installed Python.
set -euo pipefail
umask 077
BRIDGE_APP_DIR="$(cd "$(dirname "$0")" && pwd)"
BRIDGE_REPO_DIR="$(cd "$BRIDGE_APP_DIR/.." && pwd)"
BRIDGE_RUNTIME_DIR="$BRIDGE_REPO_DIR/.bridge/runtime"
BRIDGE_REQUIREMENTS="$BRIDGE_APP_DIR/requirements-desktop.txt"
BRIDGE_RUNTIME_PYTHON="$BRIDGE_RUNTIME_DIR/bin/python3"

if [ ! -f "$BRIDGE_REQUIREMENTS" ]; then
  printf '%s\n' "灵桥缺少依赖清单：$BRIDGE_REQUIREMENTS" >&2
  exit 1
fi

mkdir -p "$BRIDGE_REPO_DIR/.bridge"

# Reuse the app's own environment when its interpreter still works. This
# repairs missing packages without touching other applications' runtimes.
if [ ! -x "$BRIDGE_RUNTIME_PYTHON" ] || ! "$BRIDGE_RUNTIME_PYTHON" -c 'import ssl, sys; assert sys.version_info >= (3, 10)' >/dev/null 2>&1; then
  BRIDGE_BASE_PYTHON=""
  for BRIDGE_CANDIDATE in "${BRIDGE_PYTHON:-}" /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 || true)" /usr/bin/python3; do
    [ -x "$BRIDGE_CANDIDATE" ] || continue
    if "$BRIDGE_CANDIDATE" -c 'import ssl, venv, sys; assert sys.version_info >= (3, 10)' >/dev/null 2>&1; then
      BRIDGE_BASE_PYTHON="$BRIDGE_CANDIDATE"
      break
    fi
  done
  if [ -z "$BRIDGE_BASE_PYTHON" ]; then
    printf '%s\n' '灵桥未找到可创建独立环境的 Python 3.10 或更新版本。请安装 Homebrew Python，或用 BRIDGE_PYTHON 指定已有 Python。' >&2
    exit 1
  fi
  printf '%s\n' "正在创建灵桥独立运行环境：$BRIDGE_RUNTIME_DIR"
  "$BRIDGE_BASE_PYTHON" -m venv "$BRIDGE_RUNTIME_DIR"
fi

"$BRIDGE_RUNTIME_PYTHON" -m ensurepip --upgrade
"$BRIDGE_RUNTIME_PYTHON" -m pip install --disable-pip-version-check -r "$BRIDGE_REQUIREMENTS"
"$BRIDGE_RUNTIME_PYTHON" -c 'import webview, AppKit, Quartz, Security, WebKit'
printf '%s\n' '灵桥桌面依赖已恢复。现在可以重新打开灵桥。'
