#!/usr/bin/env bash
# 打服务源码包并给出上传命令(REQ-274 hub 产物分发)。
#
# 用法: deploy/package.sh [版本标识]     # 缺省用 git short HEAD
# 产物: dist/sqlmux-<ver>.tar.gz(包内顶层目录 sqlmux-<ver>/,worker 侧自动提升一层)
#
# 说明:sqlmux 是纯 Python 源码服务,没有编译产物;新机器要运行它只需
# 源码 + uv(uv run 按 pyproject/uv.lock 现装依赖)。二进制服务(code-map)
# 走同一通道,只是 kind=binary。
set -euo pipefail

cd "$(dirname "$0")/.."

VER="${1:-$(git rev-parse --short HEAD)}"
OUT_DIR="dist"
OUT="$OUT_DIR/sqlmux-${VER}.tar.gz"
HUB="${AGENT_PANEL_URL:-http://192.168.6.133:8788}"

mkdir -p "$OUT_DIR"
git archive --format=tar.gz --prefix="sqlmux-${VER}/" -o "$OUT" HEAD

SHA="$(sha256sum "$OUT" | cut -d' ' -f1)"
SIZE="$(stat -c%s "$OUT")"

cat <<MSG
打包完成
  file   : $OUT
  version: $VER
  sha256 : $SHA
  size   : $SIZE bytes

上传到 hub(产物落 hub state 目录,不进备份仓):
  curl -sS -X PUT "$HUB/api/service/artifact?name=sqlmux&version=${VER}&kind=source" \\
    --data-urlencode "entry=uv run --project . sqlmux serve --envs uat-cn,uat-sea,pro-cn,pro-sea" \\
    -H 'Content-Type: application/gzip' \\
    --data-binary @"$OUT"

校验:
  curl -sS "$HUB/api/service/artifact/latest?name=sqlmux"
  curl -sS "$HUB/api/service/artifact/list?name=sqlmux"
MSG
