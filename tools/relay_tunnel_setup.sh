#!/bin/bash
# relay_tunnel_setup.sh — 本机→VPS 反向隧道, 把本地中继暴露给 VPS 用
#
# 拓扑: VPS(127.0.0.1:18080) --ssh -R--> 本机(127.0.0.1:80) /relay.php
# 用法: ./build/tools/relay_tunnel_setup.sh [--check|--remove]
#   依赖: 本机已能 ssh 免密登录 VPS(密钥 ~/.ssh/id_ed25519_vps, 见 README/使用说明),
#         且本机 80 端口有能服务 relay.php 的站点(nginx)。
#   效果: 建 systemd 服务 relay-tunnel(开机自启+断线重连), VPS 上:
#         curl -sI 'http://127.0.0.1:18080/relay.php?p=/sitemap.xml&t=<token>'
set -euo pipefail
cd "$(dirname "$0")/../.."

VPS_HOST="${VPS_HOST:-root@vps.example.com}"
VPS_PORT="${VPS_PORT:-26318}"
REMOTE_PORT="${REMOTE_PORT:-18080}"
LOCAL_PORT="${LOCAL_PORT:-80}"
KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519_vps}"
UNIT=/etc/systemd/system/relay-tunnel.service
MODE="${1:-}"

if [ "$MODE" = "--remove" ]; then
    sudo systemctl disable --now relay-tunnel.service 2>/dev/null || true
    sudo rm -f "$UNIT"
    sudo systemctl daemon-reload
    echo "已移除 relay-tunnel"
    exit 0
fi

[ -f "$KEY" ] || { echo "缺密钥 $KEY（先 ssh-copy-id -i $KEY.pub -p $VPS_PORT $VPS_HOST）" >&2; exit 1; }
if ! ssh -i "$KEY" -p "$VPS_PORT" -o BatchMode=yes -o ConnectTimeout=8 "$VPS_HOST" true; then
    echo "SSH 免密不通: $VPS_HOST:$VPS_PORT" >&2
    exit 1
fi
if [ "$MODE" = "--check" ]; then
    echo "SSH 免密 OK; 单元是否已装: $([ -f "$UNIT" ] && echo yes || echo no)"
    exit 0
fi

sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=relay reverse tunnel (local:${LOCAL_PORT} -> vps:${REMOTE_PORT})
After=network-online.target nginx.service
Wants=network-online.target

[Service]
User=$(id -un)
ExecStart=/usr/bin/ssh -N -T \\
  -R 127.0.0.1:${REMOTE_PORT}:127.0.0.1:${LOCAL_PORT} \\
  -p ${VPS_PORT} -i ${KEY} \\
  -o BatchMode=yes -o ExitOnForwardFailure=yes \\
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \\
  -o StrictHostKeyChecking=accept-new ${VPS_HOST}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now relay-tunnel.service
sleep 2
sudo systemctl is-active relay-tunnel.service
echo "隧道就绪: VPS 127.0.0.1:${REMOTE_PORT} -> 本机 127.0.0.1:${LOCAL_PORT}"
echo "VPS 侧验证: curl -sI 'http://127.0.0.1:${REMOTE_PORT}/relay.php?p=/sitemap.xml&t=<token>'"
