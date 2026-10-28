#!/bin/sh
# sync_via_relay.sh — 经本地中继(relay_source.php)同步源站增量
#
# 适用: 目标站直连老站被断(ERR 28 超时)时; 依赖中继机上的反向隧道
#       relay-tunnel(中继机 80 -> 目标机 127.0.0.1:18080)。
# 用法: php 无关, 直接跑
#   cd /path/to/joomla
#   ./cli/sync_via_relay.sh                 # dry-run
#   ./cli/sync_via_relay.sh --apply         # 实写
#   ./cli/sync_via_relay.sh --apply --only=1721
# 可覆盖环境变量: PHP / TOKEN_FILE / RELAY
set -e
SITE=$(cd "$(dirname "$0")/.." && pwd)
PHP=${PHP:-/www/server/php/82/bin/php}
TOKEN_FILE=${TOKEN_FILE:-/root/.relay-token}
RELAY=${RELAY:-http://127.0.0.1:18080/relay.php}

[ -r "$TOKEN_FILE" ] || { echo "缺中继口令文件 $TOKEN_FILE" >&2; exit 1; }
exec env SXPG_RELAY_TOKEN="$(cat "$TOKEN_FILE")" \
    "$PHP" "$SITE/cli/sync_source.php" "--src-base=$RELAY" "$@"
