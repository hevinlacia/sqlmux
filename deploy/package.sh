#!/usr/bin/env bash
# 打服务源码包并给出上传命令(REQ-274 hub 产物分发)。
#
# 用法: deploy/package.sh [版本标识]     # 缺省用 git short HEAD
# 产物: dist/sqlmux-<ver>.tar.gz(包内顶层目录 sqlmux-<ver>/,worker 侧自动提升一层)
#
# 说明:sqlmux 是**纯标准库** Python 服务(零第三方依赖),没有编译产物;
# 新机器只要源码 + 任意 python3 即可运行,无需 pip/uv/venv。二进制服务
# (code-map)走同一通道,只是 kind=binary。
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

# entry 必须走 query 参数并自行 URL 编码;不能用 --data-urlencode,
# 否则文本会拼进 tar.gz body 从而损坏产物(sha256 对不上)。
ENTRY="$(python3 -c "import urllib.parse;print(urllib.parse.quote('PYTHONPATH=src python3 -m sqlmux serve --envs uat-cn,uat-sea,pro-cn,pro-sea',safe=''))")"

cat <<MSG
打包完成
  file   : $OUT
  version: $VER
  sha256 : $SHA
  size   : $SIZE bytes

上传到 hub(产物落 hub state 目录,不进备份仓):
  curl -sS -X PUT "$HUB/api/service/artifact?name=sqlmux&version=${VER}&kind=source&entry=${ENTRY}" \\
    -H 'Content-Type: application/gzip' \\
    --data-binary @"$OUT"

校验:
  curl -sS "$HUB/api/service/artifact/latest?name=sqlmux"
  curl -sS "$HUB/api/service/artifact/list?name=sqlmux"
MSG
