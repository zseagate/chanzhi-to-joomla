# 老站封服务器时的「只读中继」同步方案

割接后常见死局：**新站服务器连不上老站**（境内对境外 VPS 断 TCP、或老站封了该 IP），
但手边总有一台机器能正常访问老站（办公室/国内小机）。

这时不要把老站内容导成备份再传（延迟高、要人工），而是让那台机器做**只读中继**：
目标站按原样跑 `sync_source.php`，只是把「取源地址」换成中继。

```
老站 old-site.example.com
      ↑ 中继机出网
relay.php（只读代理：白名单 + 口令 + 限速 + 静态缓存）
      ↑ HTTP 或 SSH 反向隧道
目标站 sync_source.php --src-base=http://127.0.0.1:18080/relay.php
```

## 1. 中继机部署（一次性）

```bash
# relay.php 放到该机某个站点的根目录（能被 php-fpm 执行即可）
sudo cp tools/relay_source.php /var/www/html/relay.php

# 口令：随机 40 位，php 进程可读
sudo install -d -m 755 /etc/relay-source
head -c 32 /dev/urandom | base64 | tr -d '/+=' | cut -c1-40 | sudo tee /etc/relay-source/token
sudo chmod 644 /etc/relay-source/token

# 上游不是默认 old-site.example.com 时：
# 在 php-fpm 环境里设 RELAY_UPSTREAM=https://老站域名（或写进 relay.php 顶部常量）
```

自检：

```bash
T=$(sudo cat /etc/relay-source/token)
curl -s -o /dev/null -w '%{http_code} %{size_download}\n' "http://127.0.0.1/relay.php?p=/sitemap.xml&t=$T"
curl -s -o /dev/null -w '%{http_code}\n'                "http://127.0.0.1/relay.php?p=/etc/passwd&t=$T"   # 期望 403
```

## 2. 目标站接上中继

能直连中继机（有公网/内网地址）时最简单：

```bash
php cli/sync_source.php --src-base=https://relay.example.com/relay.php --relay-token=<token>
```

只能由中继机主动连目标站时，用反向隧道（`tools/relay_tunnel_setup.sh`，
建 systemd 服务 `relay-tunnel`，本机 80 → 目标机 `127.0.0.1:18080`，断线 5s 重连）：

```bash
# 中继机（需已能 ssh 免密登录目标机）
VPS_HOST=root@target.example.com VPS_PORT=22 ./tools/relay_tunnel_setup.sh

# 目标站
printf '%s' '<token>' > /root/.relay-token && chmod 600 /root/.relay-token
./cli/sync_via_relay.sh              # dry-run
./cli/sync_via_relay.sh --apply      # 实写
```

`sync_via_relay.sh` 只是包装：`sync_source.php --src-base=http://127.0.0.1:18080/relay.php`，
口令从 `$TOKEN_FILE` 读，站点/PHP 路径可用 `PHP=`、`RELAY=`、`TOKEN_FILE=` 覆盖。

## 3. 中继的行为边界

| 能力 | 说明 |
|---|---|
| 路径白名单 | `/sitemap.xml`、`/`、`/<栏目>.html`、`/<栏目>/<id>.html`、`/file.php`、`/file-download-*` |
| 上游固定 | 主机写死 `RELAY_UPSTREAM`，不接受调用方指定 URL（防 SSRF） |
| 认证 | `?t=<token>` 或 `X-Relay-Token`；`sync_source.php` 两种都会带 |
| 跳转 | 只跟随**站内** 301；站外 301 原样回传（站外文判定依赖它） |
| 限速 | 全局最小间隔 1s，多个同步并发也不会把老站打到限流 |
| 缓存 | `file.php` / `file-download-*` 落盘 7 天；HTML 不缓存，保证增量新鲜 |

## 4. 排障

- `403`：口令不符或路径不在白名单。
- `502`：中继机自己也出不了网（先在中继机上 `curl` 老站验证）。
- 目标站 `curl 127.0.0.1:18080` 不通：隧道断，`systemctl status relay-tunnel`；
  目标机 `ss -ltn | grep 18080` 应有监听。
- 日志：目标站 `administrator/logs/sync_source.log`；中继机 php-fpm error log。
- 轮换口令：两边文件改成同一串即可，无需重启。

## 5. 多站并存时的同步清单

同一份老站内容可能同时有 3~4 个站（生产 Joomla、复刻站、备用 WP 等），
**文章同步和轮播同步是两件事，必须分别跑**：

1. 生产站：`sync_source.php --apply`（或 `sync_via_relay.sh --apply`）。
2. 复刻/备用站：各自的 `sync_source.php --apply`（同一套口径，导入幂等）。
3. **首页轮播**：`sync_slides.php --apply`（老站幻灯是区块，文章同步不覆盖它）。
4. 若备用站是 WordPress：以 Joomla 库为源重跑迁移脚本（文章 ID 原样保留）。
5. 模板改动要同步到每一份模板副本并 bump 版本串，否则老 JS 与新模块内容不匹配
   （典型症状：所有幻灯图竖排显示）。
