<?php
/**
 * relay_source.php — 老站只读中继: 给连不上老站的服务器取源用
 *
 * 部署: 放到能出网访问老站的站点根目录(如 /var/www/html/relay.php), 加口令文件
 *   sudo install -d -m 755 /etc/relay-source
 *   head -c 32 /dev/urandom | base64 | tr -d '/+=' | cut -c1-40 | sudo tee /etc/relay-source/token
 *   sudo chmod 644 /etc/relay-source/token      # php-fpm(www-data) 可读即可
 *   RELAY_UPSTREAM 不设则默认 https://old-site.example.com
 *
 * 用法(仅 CLI 之外的 HTTP GET):
 *   GET /relay.php?p=/sitemap.xml&t=<token>
 *   GET /relay.php?p=/caijingxinwen/1720.html&t=<token>
 *   GET /relay.php?p=/file.php?f=...&t=<token>
 *
 * 调用方: 站点 cli/sync_source.php --src-base=https://<本机域名>/relay.php --relay-token=<token>
 *
 * 设计要点:
 *   - 路径白名单(只放行 sitemap/栏目页/文章页/file.php/file-download-*), 杜绝 SSRF
 *   - 上游固定(RELAY_UPSTREAM), 不接受调用方指定主机
 *   - 不跟随外部跳转: 站外 301 原样回传(正文外链文判定依赖 301 + Location)
 *   - 上游最小请求间隔 1s(全局锁), 避免把源站限流引到本机
 *   - 静态二进制(file.php / file-download-*)落盘缓存 7 天, HTML 不缓存(保证增量新鲜)
 */

// 上游老站(可用环境变量 RELAY_UPSTREAM 覆盖)
define('UPSTREAM', rtrim(getenv('RELAY_UPSTREAM') ?: 'https://old-site.example.com', '/'));
define('UPSTREAM_HOST', (string)parse_url(UPSTREAM, PHP_URL_HOST));
const UA         = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36';
const MIN_DELAY  = 1.0;   // 上游最小间隔(秒)
const MAX_BYTES  = 64 * 1024 * 1024;
const CACHE_TTL  = 604800; // 静态文件缓存 7 天

$tokenFile = getenv('RELAY_TOKEN_FILE') ?: '/etc/relay-source/token';
$token = is_readable($tokenFile) ? trim((string)file_get_contents($tokenFile)) : '';
if ($token === '') {
    http_response_code(503);
    header('Content-Type: text/plain; charset=utf-8');
    exit("relay: token file missing/unreadable ($tokenFile)\n");
}
$got = isset($_SERVER['HTTP_X_RELAY_TOKEN']) ? (string)$_SERVER['HTTP_X_RELAY_TOKEN']
     : (isset($_GET['t']) ? (string)$_GET['t'] : '');
if ($got === '' || !hash_equals($token, $got)) {
    http_response_code(403);
    header('Content-Type: text/plain; charset=utf-8');
    exit("relay: forbidden\n");
}

/* ---------------- 路径校验(白名单) ---------------- */
$p = isset($_GET['p']) ? (string)$_GET['p'] : '';
if ($p === '' || $p[0] !== '/' || preg_match('#[\x00-\x1f\\\\]#', $p) || strpos($p, '..') !== false) {
    http_response_code(400);
    exit("relay: bad path\n");
}
$pathOnly = strtok($p, '?');
$allowed = [
    '#^/$#',                                  // 首页(轮播区块)
    '#^/sitemap\.xml$#',
    '#^/[a-z][a-z0-9_-]*\.html$#',            // 栏目落地页
    '#^/[a-z][a-z0-9_-]*/\d+\.html$#',        // 文章页 /<slug>/<oldid>.html
    '#^/file\.php$#',
    '#^/file-download-[A-Za-z0-9_.\-]+$#',
];
$ok = false;
foreach ($allowed as $re) {
    if (preg_match($re, $pathOnly)) {
        $ok = true;
        break;
    }
}
if (!$ok) {
    http_response_code(403);
    exit("relay: path not allowed\n");
}

$url = UPSTREAM . $p;

/* ---------------- 静态资源缓存 ---------------- */
$isAsset = (bool)preg_match('#^/(file\.php|file-download-)#', $pathOnly);
$cacheDir = rtrim(sys_get_temp_dir(), '/') . '/relay_source_cache';
$cacheKey = $cacheDir . '/' . sha1($url);
if ($isAsset && is_file($cacheKey) && (time() - filemtime($cacheKey)) < CACHE_TTL) {
    $meta = json_decode((string)@file_get_contents($cacheKey . '.json'), true);
    if (is_array($meta)) {
        http_response_code((int)($meta['code'] ?? 200));
        header('Content-Type: ' . ($meta['type'] ?? 'application/octet-stream'));
        header('X-Relay-Cache: HIT');
        readfile($cacheKey);
        exit;
    }
}

/* ---------------- 上游限速(全局) ---------------- */
$lockPath = $cacheDir . '/rate.lock';
if (!is_dir($cacheDir)) {
    @mkdir($cacheDir, 0750, true);
}
$lf = @fopen($lockPath, 'c');
if ($lf) {
    flock($lf, LOCK_EX);
    $tsFile = $cacheDir . '/rate.ts';
    $last = is_file($tsFile) ? (float)file_get_contents($tsFile) : 0.0;
    $wait = MIN_DELAY - (microtime(true) - $last);
    if ($wait > 0) {
        usleep((int)($wait * 1e6));
    }
    @file_put_contents($tsFile, (string)microtime(true));
    flock($lf, LOCK_UN);
    fclose($lf);
}

/* ---------------- 抓取(3 跳以内, 仅站内跳转) ---------------- */
function fetch(string $url, int &$hops): array
{
    $hops = 0;
    for ($i = 0; $i < 4; $i++) {
        $ch = curl_init($url);
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_FOLLOWLOCATION => false,
            CURLOPT_TIMEOUT        => 90,
            CURLOPT_CONNECTTIMEOUT => 10,
            CURLOPT_USERAGENT      => UA,
            CURLOPT_IPRESOLVE      => CURL_IPRESOLVE_V4,
            CURLOPT_ACCEPT_ENCODING=> '',
            CURLOPT_HTTPHEADER     => ['Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'],
        ]);
        $body = curl_exec($ch);
        $errno = curl_errno($ch);
        $code  = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $type  = (string)curl_getinfo($ch, CURLINFO_CONTENT_TYPE);
        $loc   = (string)curl_getinfo($ch, CURLINFO_REDIRECT_URL);
        curl_close($ch);
        if ($errno !== 0 || $body === false) {
            return [0, '', '', ''];
        }
        // 站内跳转继续跟(最多 3 次); 站外跳转原样回传
        $internal = $loc !== '' && (
            ($loc[0] === '/') ||
            (stripos($loc, 'https://' . UPSTREAM_HOST) === 0) ||
            (stripos($loc, 'http://' . UPSTREAM_HOST) === 0)
        );
        if ($code >= 300 && $code < 400 && $loc !== '' && $internal) {
            $hops++;
            $url = ($loc[0] === '/') ? UPSTREAM . $loc : $loc;
            continue;
        }
        return [$code, (string)$body, $type, $loc];
    }
    return [0, '', '', ''];
}

$hops = 0;
[$code, $body, $type, $loc] = fetch($url, $hops);
if ($code === 0) {
    http_response_code(502);
    header('Content-Type: text/plain; charset=utf-8');
    exit("relay: upstream error\n");
}
if (strlen($body) > MAX_BYTES) {
    http_response_code(413);
    exit("relay: response too large\n");
}

http_response_code($code);
if ($loc !== '') {
    header('Location: ' . $loc);
}
if ($type !== '') {
    header('Content-Type: ' . $type);
} elseif ($isAsset) {
    header('Content-Type: application/octet-stream');
}
header('X-Relay: 1' . ($hops ? ' +' . $hops : ''));
echo $body;

/* 静态资源落盘(仅成功且非 HTML) */
if ($isAsset && $code === 200 && strlen($body) >= 200 && stripos((string)$type, 'text/html') === false) {
    @file_put_contents($cacheKey, $body, LOCK_EX);
    @file_put_contents($cacheKey . '.json', json_encode(['code' => $code, 'type' => $type]), LOCK_EX);
}
