#!/usr/bin/env php
<?php
/**
 * sync_source.php — 老蝉知站 → Joomla 割接后持续增量同步(单文件, 部署在站点 cli/ 目录)。
 *
 * 双通道:
 *   A. HTTP 抓取(默认): 老站 sitemap.xml 全量发现 + 栏目落地页交叉归属 + 文章页解析。
 *   B. 备份导入(--bundle=DIR): 零访问老站, 导入 sync_bundle_from_backup.py 生成的增量包。
 *      (老站限流/封 IP/服务器在境外连不上时走这条路)
 *
 * 用法(SSH 到站点根目录):
 *   php cli/sync_source.php                          # dry-run: 只打印计划, 不写库不下载
 *   php cli/sync_source.php --apply                  # 实写(每篇文章一个事务, 自动写后校验)
 *   可选: --bundle=DIR 从备份增量包导入 | --limit=N | --only=<老id,...> | --no-landing
 *         --timeout=N | --proxy=socks5h://127.0.0.1:1080 (境外服务器经反向隧道访问老站)
 *
 * 兼容 PHP 7.2.5+(与 Joomla 4 一致, 未用 8.x 专有语法)。
 *
 * 写库口径(与 chanzhi_to_joomla.py 迁移一致):
 *   jos_content + jos_assets(parent_id=com_content资产, lft/rgt=MAX+1成对)
 *   + jos_workflow_associations(stage 1, 缺了后台列表不可见)
 *   + jos_redirect_links(老 URL 301, 需启用「System - Redirect」插件)
 *   alias = c<栏目id>-<老文章id>; 主栏目 = 落地页链接指向的栏目(CMS 规范链接);
 *   交叉发布自动补副本行(featured=0 不写301); 站外文(link 非空)标题直跳外链。
 *
 * ⚠ 频控教训: 老站对高频抓取会封 IP(TCP 级)。每天最多一轮全量; 只想看有没有新文章
 *   用 --no-landing(1 个请求); 封了就走 bundle 通道, 别硬抓。
 */

const _JEXEC = 1;
define('JPATH_BASE', dirname(__DIR__));
require_once JPATH_BASE . '/includes/defines.php';
require_once JPATH_BASE . '/includes/framework.php';

use Joomla\CMS\Factory;
use Joomla\Database\DatabaseInterface;

if (PHP_SAPI !== 'cli') {
    die("CLI only\n");
}

/* ===================== 站点配置(按你的站改这里) ===================== */

// 老站(蝉知)地址
const SRC_BASE = 'https://old-site.example.com';
const SRC_HOST = 'old-site.example.com';

// 栏目 slug(老站 URL 路径名) => [Joomla catid, 名称(仅打印用)]
// 对照迁移时的 ALIVE_ORDER/JCAT_BASE 逐个填
const CHANNELS = [
    'news'     => [1001, 'News'],
    'notices'  => [1002, 'Notices'],
    'briefs'   => [1003, 'Briefs'],
    'training' => [1004, 'Training'],
];

// 老站 /file.php 链接改写后的 URL 前缀(= 迁移 ETL 的 IMAGE_BASE), 文件落在 <站点根>/upload/ 子路径
const IMG_URL = '/images/site';

// jos_assets 里 com_content 的资产 id(SELECT id FROM jos_assets WHERE name='com_content')
const ASSET_PARENT = 8;

// 新文章 created_by 对应的用户名
const AUTHOR_USER = 'admin';

/* ===================== 一般不用改 ===================== */

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36';
const REQ_DELAY = 2.0;   // 相邻请求最小间隔(秒), 防老站限流
const TPL_EXT   = ['jpg', 'jpeg', 'png', 'gif', 'webp'];
const DOC_EXT   = ['doc', 'docx', 'pdf', 'xls', 'xlsx', 'ppt', 'wps', 'txt', 'zip', 'rar'];
const FILE_RE   = '#/file\.php\?f=([^"\'>\s<>]+)((?:&[^"\'>\s<>]*)?)#';

/* ----------------------------- 参数/锁/日志 ----------------------------- */

$opt = getopt('', ['apply', 'limit:', 'only:', 'no-landing', 'timeout:', 'proxy:', 'bundle:']);
$APPLY    = isset($opt['apply']);
$NO_LAND  = isset($opt['no-landing']);
$ONLY     = isset($opt['only']) ? array_map('intval', explode(',', (string)$opt['only'])) : [];
$LIMIT    = isset($opt['limit']) ? max(1, (int)$opt['limit']) : 0;
$HTTP_TIMEOUT = isset($opt['timeout']) ? max(10, (int)$opt['timeout']) : 40;
// 境外服务器直连老站被断时: 国内机器 ssh -R 1080 user@server 建反向 SOCKS 隧道后
// 用 --proxy=socks5h://127.0.0.1:1080 (也支持 http:// 代理)
$PROXY    = isset($opt['proxy']) ? (string)$opt['proxy'] : '';
// 备份通道: 导入 sync_bundle_from_backup.py 生成的增量包目录(bundle.json + files/)
$BUNDLE   = isset($opt['bundle']) ? rtrim((string)$opt['bundle'], '/') : '';

// 锁文件: tmp/ 目录可能不存在, 自动创建; 再不行退回系统临时目录
$lockDir = JPATH_BASE . '/tmp';
if (!is_dir($lockDir)) {
    @mkdir($lockDir, 0755, true);
}
$lockPath = $lockDir . '/sync_source.lock';
$lockF = @fopen($lockPath, 'c');
if (!$lockF) {
    $lockPath = sys_get_temp_dir() . '/chanzhi_sync_source.lock';
    $lockF = @fopen($lockPath, 'c');
}
if (!$lockF) {
    die("[abort] 无法创建锁文件, 请检查目录权限\n");
}
if (!flock($lockF, LOCK_EX | LOCK_NB)) {
    die("[abort] 已有同步进程在运行($lockPath)\n");
}

$logPath = rtrim((string)Factory::getConfig()->get('log_path'), '/') . '/sync_source.log';
if (!is_dir(dirname($logPath))) {
    @mkdir(dirname($logPath), 0755, true);
}
function out(string $msg, bool $toLog = true): void
{
    global $logPath;
    $line = '[' . date('Y-m-d H:i:s') . '] ' . $msg;
    echo $line, "\n";
    if ($toLog) {
        @file_put_contents($logPath, $line . "\n", FILE_APPEND | LOCK_EX);
    }
}

/* ----------------------------- HTTP ----------------------------- */

function httpGet(string $url, bool $follow = true, int $timeout = 0): array
{
    global $HTTP_TIMEOUT;
    if ($timeout <= 0) {
        $timeout = $HTTP_TIMEOUT;
    }
    static $lastTs = 0.0;
    $wait = REQ_DELAY - (microtime(true) - $lastTs);
    if ($wait > 0) {
        usleep((int)($wait * 1e6));
    }
    for ($i = 1; $i <= 3; $i++) {
        $ch = curl_init($url);
        $copts = [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_FOLLOWLOCATION => $follow,
            CURLOPT_MAXREDIRS      => 4,
            CURLOPT_TIMEOUT        => $timeout,        // 总时长
            CURLOPT_CONNECTTIMEOUT => 10,              // 连接阶段快速失败, 让重试有意义
            CURLOPT_USERAGENT      => UA,
        ];
        if (defined('CURLOPT_IPRESOLVE')) {
            $copts[CURLOPT_IPRESOLVE] = CURL_IPRESOLVE_V4;
        }
        if (defined('CURLOPT_ACCEPT_ENCODING')) {
            $copts[CURLOPT_ACCEPT_ENCODING] = ''; // 自动处理 gzip(大文件显著提速)
        }
        global $PROXY;
        if ($PROXY !== '') {
            $copts[CURLOPT_PROXY] = $PROXY;
        }
        curl_setopt_array($ch, $copts);
        $body = curl_exec($ch);
        $errno = curl_errno($ch);
        $code  = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $loc   = (string)curl_getinfo($ch, CURLINFO_REDIRECT_URL); // 不跟随重定向时的下一跳
        curl_close($ch);
        $lastTs = microtime(true);
        if ($errno === 0 && $body !== false && $code > 0) {
            return [$code, (string)$body, $loc];
        }
        if ($i < 3) {
            out("    http 重试($i): $url (errno=$errno)");
            sleep(6 * $i);
        }
    }
    return [0, '', ''];
}

/* ----------------------------- 解析 ----------------------------- */

/** 老站 sitemap.xml → [老id => [slug, lastmod]] (只收 .html 桌面版) */
function parseSitemap(string $xml): array
{
    $out = [];
    if (preg_match_all('#/([a-z0-9_-]+)/(\d+)\.html\s*</loc>\s*<lastmod>\s*(\d{4}-\d{2}-\d{2})#s', $xml, $ms, PREG_SET_ORDER)) {
        foreach ($ms as $m) {
            $out[(int)$m[2]] = [$m[1], $m[3]];
        }
    }
    return $out;
}

/** 栏目落地页 → [老id => ['href','title','date','views']] */
function parseLanding(string $html): array
{
    $out = [];
    if (!preg_match_all("#<div class='item' id=\"article(\d+)\"[^>]*>(.*?)(?=<div class='item' id=\"article|</section|$)#s", $html, $ms, PREG_SET_ORDER)) {
        return $out;
    }
    foreach ($ms as $m) {
        $id = (int)$m[1];
        $blk = $m[2];
        $href = $title = $date = '';
        $views = 0;
        if (preg_match("#<a href='([^']+)'#", $blk, $h)) {
            $href = $h[1];
        }
        if (preg_match('#<h4>\s*<a[^>]*>(.*?)</a>#s', $blk, $t)) {
            $title = trim(preg_replace('/\s+/', ' ', strip_tags($t[1])));
        }
        if (preg_match('#title="发布时间".*?</i>\s*(\d{4}-\d{2}-\d{2})#s', $blk, $d)) {
            $date = $d[1];
        }
        if (preg_match('#title="(\d+)"#', $blk, $v)) {
            $views = (int)$v[1];
        }
        $out[$id] = ['href' => $href, 'title' => $title, 'date' => $date, 'views' => $views];
    }
    return $out;
}

/** 文章页 → 标题/日期/作者/阅读数/正文/附件; 无 article-content 返回 null */
function parseArticle(string $html): ?array
{
    if (!preg_match("#<section class=['\"]article-content['\"]>(.*?)</section>#s", $html, $m)) {
        return null;
    }
    $a = ['title' => '', 'date' => '', 'author' => '', 'views' => 0,
          'body' => trim($m[1]), 'files' => []];
    if (preg_match('#<h1>(.*?)</h1>#s', $html, $t)) {
        $a['title'] = trim(preg_replace('/\s+/', ' ', strip_tags($t[1])));
    }
    if (preg_match('#添加时间：</strong>\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})#u', $html, $d)) {
        $a['date'] = $d[1];
    } elseif (preg_match('#(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})#', $html, $d)) {
        $a['date'] = $d[1];
    }
    if (preg_match('#作者：</strong>\s*([^&<]+)#u', $html, $au)) {
        $a['author'] = trim($au[1]);
    }
    if (preg_match('#阅读：</strong>\s*(\d+)#u', $html, $v)) {
        $a['views'] = (int)$v[1];
    }
    if (preg_match("#<section class=['\"]article-files['\"]>(.*?)</section>#s", $html, $fs)) {
        if (preg_match_all("#<a href='(/file-download-[^']+)'([^>]*)>#s", $fs[1], $fms, PREG_SET_ORDER)) {
            foreach ($fms as $fm) {
                $href = $fm[1];
                $attrs = $fm[2];
                $hex = preg_match("#(f_[a-f0-9]{16,64})#", $attrs, $hx) ? $hx[1] : '';
                $title = preg_match("#title='([^']*)'#", $attrs, $tt) ? $tt[1] : '';
                $ext = strtolower(pathinfo($title, PATHINFO_EXTENSION));
                // 图片附件通常已在正文里且默认隐藏, 只追加文档类
                if ($hex !== '' && $ext !== '' && in_array($ext, DOC_EXT, true)) {
                    $a['files'][] = ['href' => $href, 'hex' => $hex, 'title' => $title, 'ext' => $ext];
                }
            }
        }
    }
    return $a;
}

/** 正文内链改写: file.php → IMG_URL/upload/<f>[.ext]; $dl[staged]=老站完整URL */
function rewriteBody(string $html, array &$dl): string
{
    return preg_replace_callback(FILE_RE, function ($m) use (&$dl) {
        $fp   = $m[1];
        $tail = str_replace('&amp;', '&', $m[2]);
        $t = '';
        if (preg_match('/[?&]t=([a-zA-Z0-9]+)/', $tail, $tm)) {
            $t = strtolower($tm[1]);
        }
        $ext  = strtolower(pathinfo($fp, PATHINFO_EXTENSION));
        $path = ($ext !== '' && in_array($ext, TPL_EXT, true))
              ? $fp
              : $fp . '.' . (in_array($t, TPL_EXT, true) ? $t : 'jpg');
        $staged = 'upload/' . $path;
        $dl[$staged] = SRC_BASE . '/file.php?f=' . $fp . $tail;
        return IMG_URL . '/' . $staged;
    }, $html);
}

function isExternalUrl(string $url): bool
{
    if ($url === '' || $url[0] === '/') {
        return false;
    }
    return stripos($url, SRC_HOST) === false;
}

/* ----------------------------- 主流程 ----------------------------- */

try {
    $db = Factory::getContainer()->get(DatabaseInterface::class);

    out("=== 老站增量同步开始 " . ($APPLY ? '[APPLY 实写]' : '[dry-run 预览]') . ($BUNDLE ? " [bundle={$BUNDLE}]" : '') . " ===");

    /* 1. 本地已有老id集(alias 反解, 自愈无状态文件, 重跑续传) */
    $known = [];   // 老id => [jcat => content_id]
    $rows  = $db->setQuery('SELECT id, alias FROM #__content WHERE alias REGEXP \'^c[0-9]+-[0-9]+$\'')->loadAssocList();
    foreach ((array)$rows as $r) {
        if (preg_match('/^c(\d+)-(\d+)$/', $r['alias'], $m)) {
            $known[(int)$m[2]][(int)$m[1]] = (int)$r['id'];
        }
    }
    out("本地已有文章 oldid 数: " . count($known));

    if ($BUNDLE !== '') {
        /* ===== 备份通道: 从 bundle.json 构造计划, 全程不访问老站 ===== */
        $bj = $BUNDLE . '/bundle.json';
        if (!is_file($bj)) {
            throw new RuntimeException("bundle.json 不存在: $bj");
        }
        $meta = json_decode((string)file_get_contents($bj), true);
        if (!is_array($meta) || empty($meta['records'])) {
            out("bundle 无待导入记录, 结束");
            exit(0);
        }
        out("bundle: 生成于 " . ($meta['generated'] ?? '?') . ", " . count($meta['records']) . ' 篇 (源: ' . ($meta['sql'] ?? '?') . ')');
        $plans = [];
        foreach ($meta['records'] as $rec) {
            $oid = (int)$rec['oldid'];
            if ($LIMIT && count($plans) >= $LIMIT) {
                break;
            }
            if ($ONLY && !in_array($oid, $ONLY, true)) {
                continue;
            }
            if (isset($known[$oid][(int)$rec['primary']])) {
                // 主行已存在: 只补缺失的副本栏目
                $missingCopies = [];
                foreach ($rec['copies'] as $cj) {
                    if (!isset($known[$oid][(int)$cj])) {
                        $missingCopies[] = (int)$cj;
                    }
                }
                if (!$missingCopies) {
                    out("  [skip] $oid 已存在且副本齐全");
                    continue;
                }
                $rec['copies'] = $missingCopies;
                $rec['_mainExists'] = true;
            }
            $rec['oid'] = $oid;
            $plans[] = $rec;
        }
    } else {
        /* ===== HTTP 通道 ===== */
    /* 2. 老站 sitemap 全量(单请求拿到全部文章 URL, 绕开落地页只显示最新几条的限制) */
    [$code, $xml] = httpGet(SRC_BASE . '/sitemap.xml', true, 90);
    if ($code !== 200) {
        throw new RuntimeException("sitemap.xml 获取失败 http=$code (老站封禁? 走 --bundle 通道)");
    }
    $sm = parseSitemap($xml);
    out("sitemap 收录文章: " . count($sm) . ' (最新 oldid=' . (count($sm) ? max(array_keys($sm)) : 0) . ')');
    foreach ($sm as $oid => $slug) {
        if (!isset(CHANNELS[$slug[0]])) {
            unset($sm[$oid]); // 单页等非文章频道
        }
    }

    /* 3. 栏目落地页(交叉发布归属 + 站外文标题/外链; 每页只有最新几条) */
    $land = [];    // 老id => [slug => item]
    if (!$NO_LAND) {
        foreach (CHANNELS as $slug => [$catid, $cn]) {
            [$c2, $lh] = httpGet(SRC_BASE . '/' . $slug . '.html');
            if ($c2 !== 200) {
                out("  [warn] 落地页 $slug http=$c2, 跳过(该栏目交叉信息缺失)");
                continue;
            }
            $items = parseLanding($lh);
            foreach ($items as $oid => $item) {
                $land[$oid][$slug] = $item;
            }
            out("  落地页 $slug($cn): " . count($items) . ' 条');
        }
    }

    /* 4. 候选 = (sitemap ∪ 落地页) − 本地已有 */
    $cand = array_unique(array_merge(array_keys($sm), array_keys($land)));
    $cand = array_values(array_diff($cand, array_keys($known)));
    sort($cand);
    if ($ONLY) {
        $cand = array_values(array_intersect($cand, $ONLY));
    }
    if ($LIMIT) {
        $cand = array_slice($cand, 0, $LIMIT);
    }
    out("待同步新文章: " . count($cand) . ($cand ? ' (' . implode(',', $cand) . ')' : ''));
    if (!$cand) {
        out("=== 无新文章, 结束 ===");
        exit(0);
    }

    /* 5. 逐篇抓取分析 */
    $plans = [];
    foreach ($cand as $oid) {
        $p = ['oid' => $oid, 'title' => '', 'primary' => 0, 'copies' => [],
              'ext' => false, 'urla' => '', 'body' => '', 'date' => '',
              'author' => '', 'views' => 0, 'dl' => [], 'smSlug' => '', 'landSlugs' => []];

        // 主栏目: 落地页 item 链接的 slug(CMS 规范链接); 兜底 sitemap slug
        $hrefSlug = '';
        foreach ($land[$oid] ?? [] as $lslug => $item) {
            $p['landSlugs'][] = $lslug;
            if (preg_match('#^/([a-z0-9_-]+)/(\d+)\.html$#', $item['href'], $hm)) {
                if ((int)$hm[2] === $oid && isset(CHANNELS[$hm[1]]) && $hrefSlug === '') {
                    $hrefSlug = $hm[1];
                }
            }
        }
        $smSlug = $sm[$oid][0] ?? '';
        $p['smSlug'] = $smSlug;
        $primary = $hrefSlug !== '' ? $hrefSlug : $smSlug;
        if ($primary === '' || !isset(CHANNELS[$primary])) {
            out("  [skip] $oid 无法确定主栏目(hrefSlug=$hrefSlug smSlug=$smSlug)");
            continue;
        }
        $p['primary'] = CHANNELS[$primary][0];

        // 站外文: 落地页链接直接是外链
        $extHref = '';
        foreach ($land[$oid] ?? [] as $item) {
            if (isExternalUrl($item['href'])) {
                $extHref = $item['href'];
                break;
            }
        }

        if ($extHref !== '') {
            $p['ext'] = true;
            $p['urla'] = $extHref;
            $p['title'] = $land[$oid] ? reset($land[$oid])['title'] : '';
            $d = $land[$oid] ? reset($land[$oid])['date'] : ($sm[$oid][1] ?? '');
            $p['date'] = ($d !== '' ? $d : date('Y-m-d')) . ' 00:00:00';
        } else {
            [$ac, $ah, $loc] = httpGet(SRC_BASE . "/$primary/$oid.html", false);
            if ($ac === 301 || $ac === 302) {
                if (isExternalUrl($loc)) {
                    $p['ext'] = true;
                    $p['urla'] = $loc;
                    $p['title'] = $land[$oid] ? reset($land[$oid])['title'] : '';
                    $d = $land[$oid] ? reset($land[$oid])['date'] : ($sm[$oid][1] ?? '');
                    $p['date'] = ($d !== '' ? $d : date('Y-m-d')) . ' 00:00:00';
                } else {
                    out("  [skip] $oid 文章页重定向到非外链: $loc");
                    continue;
                }
            } elseif ($ac === 200) {
                $art = parseArticle($ah);
                if ($art === null) {
                    out("  [skip] $oid 文章页无 article-content(且无外链重定向), 请人工核看");
                    continue;
                }
                $p['title'] = $art['title'] !== '' ? $art['title'] : ($land[$oid] ? reset($land[$oid])['title'] : '');
                $d = $art['date'] !== '' ? $art['date'] : (($land[$oid] ? reset($land[$oid])['date'] : ($sm[$oid][1] ?? '')) . ' 00:00:00');
                $p['date'] = $d !== '' ? $d : date('Y-m-d H:i:s');
                $p['author'] = $art['author'] !== '' ? $art['author'] : 'admin';
                $p['views'] = $art['views'];
                $body = rewriteBody($art['body'], $p['dl']);
                // 文档附件: 下载 file-download 路由(200 直出文件), 落点 upload/<同步月>/f_<hex>.<ext>
                $attHtml = '';
                foreach ($art['files'] as $f) {
                    $staged = 'upload/' . date('Ym') . '/' . $f['hex'] . '.' . $f['ext'];
                    $p['dl'][$staged] = SRC_BASE . $f['href'];
                    $attHtml .= '<li><a href="' . IMG_URL . '/' . $staged . '" download="' . htmlspecialchars($f['title'], ENT_QUOTES) . '">' . htmlspecialchars($f['title'], ENT_QUOTES) . '</a></li>';
                }
                if ($attHtml !== '') {
                    $body .= '<h3>附件下载</h3><ul>' . $attHtml . '</ul>';
                }
                $p['body'] = $body;
            } else {
                out("  [skip] $oid 文章页 http=$ac");
                continue;
            }
        }
        if ($p['title'] === '') {
            $p['title'] = '外链文章' . $oid;
            out("  [warn] $oid 无标题来源, 兜底『{$p['title']}』");
        }

        // 交叉副本栏目 = (出现过的落地页 ∪ href ∪ sitemap) − 主栏目, 且排除本地已有
        $cats = [$primary];
        foreach ($p['landSlugs'] as $ls) {
            if (!in_array($ls, $cats, true)) {
                $cats[] = $ls;
            }
        }
        if ($smSlug !== '' && !in_array($smSlug, $cats, true)) {
            $cats[] = $smSlug;
        }
        array_shift($cats);
        foreach ($cats as $cs) {
            if (isset(CHANNELS[$cs])) {
                $jcat = CHANNELS[$cs][0];
                if (!isset($known[$oid][$jcat])) {
                    $p['copies'][] = $jcat;
                }
            }
        }
        $plans[] = $p;
    }
    } // end HTTP 通道

    /* 字段规范化(bundle 与 HTTP 两通道对齐) */
    foreach ($plans as &$p) {
        $p['featured'] = (int)($p['featured'] ?? 0);
        $p['metakey']  = $p['metakey'] ?? '';
        $p['metadesc'] = $p['metadesc'] ?? '';
        $p['smSlug']   = $p['smSlug'] ?? '';
        $p['fallback'] = $p['fallback'] ?? [];
        if ($BUNDLE !== '') {
            $p['dl'] = [];
            foreach ($p['files'] ?? [] as $st) {
                $p['dl'][$st] = 'bundle';
            }
        }
    }
    unset($p);

    /* 6. 打印计划 */
    $slugOfCat = [];
    foreach (CHANNELS as $s => [$cid2]) {
        $slugOfCat[$cid2] = $s;
    }
    out("--- 同步计划(" . count($plans) . " 篇) ---");
    $nFiles = 0;
    foreach ($plans as $p) {
        $cats = $p['primary'] . ($p['copies'] ? '+' . implode(',', $p['copies']) : '');
        $att = 0;
        if ($BUNDLE === '') {
            foreach ($p['dl'] as $u) {
                if (strpos($u, '/file-download-') !== false) {
                    $att++;
                }
            }
        }
        $img = count($p['dl']) - $att;
        $nFiles += count($p['dl']);
        out(sprintf('  %d | %s%s%s | 栏目 %s | %s | %s | %s',
            $p['oid'], $p['ext'] ? '[外链] ' : '', mb_substr($p['title'], 0, 38),
            $p['featured'] ? '[置顶] ' : '', $cats, $p['date'],
            $BUNDLE === '' ? sprintf('图%d 附件%d', $img, $att) : sprintf('文件%d', $img),
            $p['urla'] ?: ''));
    }
    out(sprintf('合计: 待落盘文件 %d 项', $nFiles));

    if (!$APPLY) {
        out("=== dry-run 结束(未写库未下载)。确认无误后加 --apply 执行 ===");
        exit(0);
    }

    /* 7. 预检: redirect 插件 / 作者用户 */
    $plug = (int)$db->setQuery("SELECT enabled FROM #__extensions WHERE type='plugin' AND folder='system' AND element='redirect'")->loadResult();
    if ($plug !== 1) {
        out("[warn] 「System - Redirect」插件未启用, redirect_links 写入不会生效(301 缺失), 请到后台启用");
    }
    $createdBy = (int)$db->setQuery('SELECT id FROM #__users WHERE username=' . $db->quote(AUTHOR_USER))->loadResult();
    if (!$createdBy) {
        $createdBy = (int)$db->setQuery('SELECT MIN(id) FROM #__users')->loadResult();
        out("[warn] 未找到用户 " . AUTHOR_USER . ", created_by 用最小用户 id");
    }

    /* 8. 执行: 文件先就位, 后逐篇事务写库 */
    $nextId    = (int)$db->setQuery('SELECT COALESCE(MAX(id),0) FROM #__content')->loadResult() + 1;
    $nextAsset = (int)$db->setQuery('SELECT COALESCE(MAX(id),0) FROM #__assets')->loadResult() + 1;
    $nextLft   = (int)$db->setQuery('SELECT COALESCE(MAX(lft),0) FROM #__assets')->loadResult() + 1;
    out("ID 起点: content=$nextId assets=$nextAsset lft=$nextLft");

    $imgRoot = JPATH_BASE . IMG_URL;
    $done = $fail = 0;
    foreach ($plans as $p) {
        $oid = $p['oid'];

        // 8.1 文件就位(bundle: 从增量包拷贝; HTTP: 从老站下载; 失败<200B 回退老站URL)
        foreach ($p['dl'] as $staged => $url) {
            $dst = $imgRoot . '/' . $staged;
            if (is_file($dst) && filesize($dst) >= 200) {
                continue;
            }
            @mkdir(dirname($dst), 0755, true);
            if ($BUNDLE !== '') {
                $src = $BUNDLE . '/files/' . $staged;
                if (is_file($src) && filesize($src) >= 200) {
                    copy($src, $dst);
                    out("  文件 OK $staged (bundle)");
                } else {
                    $fb = $p['fallback'][$staged] ?? '';
                    if ($fb !== '' && !$p['ext']) {
                        $p['body'] = str_replace(IMG_URL . '/' . $staged, $fb, (string)$p['body']);
                    }
                    out("  [warn] bundle 缺文件 $staged" . ($fb !== '' ? ', 正文回退老站URL' : ', 无回退URL(正文或留坏链)'));
                }
                continue;
            }
            [$dc, $bin] = httpGet($url, true, 60);
            if ($dc === 200 && strlen($bin) >= 200) {
                file_put_contents($dst, $bin);
                out("  文件 OK $staged (" . strlen($bin) . 'B)');
            } else {
                unset($p['dl'][$staged]);
                $p['body'] = str_replace(IMG_URL . '/' . $staged, $url, (string)$p['body']);
                out("  [warn] 文件下载失败回退老站URL: $url");
            }
        }

        // 8.2 一个事务写完一篇文章的全部行
        $db->transactionStart();
        try {
            $rowsSpec = []; // 每行: [catid, isCopy]
            $rowsSpec[] = [$p['primary'], false];
            foreach ($p['copies'] as $cj) {
                $rowsSpec[] = [$cj, true];
            }
            $firstId = 0;
            foreach ($rowsSpec as [$catid, $isCopy]) {
                $alias = 'c' . $catid . '-' . $oid;
                if ((int)$db->setQuery('SELECT COUNT(*) FROM #__content WHERE alias=' . $db->quote($alias))->loadResult() > 0) {
                    out("  [skip] alias 已存在 $alias(重跑续传)");
                    continue;
                }
                $urls  = '{}';
                $attrs = '{}';
                $note  = '';
                $full  = $p['body'];
                if ($p['ext']) {
                    $urls  = '{"urla": ' . json_encode($p['urla']) . ', "urlatext": ' . json_encode($p['title']) . ', "targeta": 1}';
                    $attrs = '{"sxpg_ext":"1"}';
                    $note  = 'external';
                    $full  = '';
                }
                $cid = $nextId++;
                $aid = $nextAsset;
                $nextAsset++;
                $lft = $nextLft;
                $nextLft += 2;

                $db->setQuery('INSERT INTO #__content '
                    . '(id, asset_id, title, alias, introtext, `fulltext`, state, catid, created, created_by, created_by_alias, '
                    . 'modified, modified_by, publish_up, publish_down, images, urls, attribs, version, ordering, '
                    . 'metakey, metadesc, access, hits, metadata, featured, language, note) VALUES ('
                    . implode(',', [$cid, $aid, $db->quote($p['title']), $db->quote($alias), $db->quote(''), $db->quote($full),
                        1, $catid, $db->quote($p['date']), $createdBy, $db->quote($p['author']),
                        $db->quote($p['date']), 0, $db->quote($p['date']), 'NULL', $db->quote('{}'), $db->quote($urls), $db->quote($attrs), 1, 0,
                        $db->quote($p['metakey']), $db->quote($p['metadesc']), 1, (int)$p['views'], $db->quote('{}'),
                        $isCopy ? 0 : (int)$p['featured'], $db->quote('*'), $db->quote($note)]) . ')')->execute();
                $db->setQuery('INSERT INTO #__assets (id, parent_id, lft, rgt, level, name, title, rules) VALUES ('
                    . implode(',', [$aid, ASSET_PARENT, $lft, $lft + 1, 3,
                        $db->quote('com_content.article.' . $cid), $db->quote($p['title']), $db->quote('')]) . ')')->execute();
                $db->setQuery('INSERT INTO #__workflow_associations (item_id, stage_id, extension) VALUES (' . $cid . ", 1, 'com_content.article')")->execute();
                if (!$isCopy) {
                    $firstId = $cid;
                }
            }

            // 301: 主 URL 必写; sitemap slug 不同则一并补(两条老地址都指向新文章)
            if ($firstId) {
                $targets = ['/' . $slugOfCat[$p['primary']] . '/' . $oid . '.html'];
                if ($p['smSlug'] !== '' && CHANNELS[$p['smSlug']][0] !== $p['primary']) {
                    $targets[] = '/' . $p['smSlug'] . '/' . $oid . '.html';
                }
                foreach ($targets as $oldUrl) {
                    if ((int)$db->setQuery('SELECT COUNT(*) FROM #__redirect_links WHERE old_url=' . $db->quote($oldUrl))->loadResult() > 0) {
                        continue;
                    }
                    $newUrl = $p['ext'] ? $p['urla'] : ('index.php?option=com_content&view=article&id=' . $firstId);
                    $db->setQuery('INSERT INTO #__redirect_links (old_url, new_url, referer, comment, hits, published, created_date, modified_date, header) VALUES ('
                        . implode(',', [$db->quote($oldUrl), $db->quote($newUrl), $db->quote(''), $db->quote(''), 0, 1, 'NOW()', 'NOW()', 301]) . ')')->execute();
                }
            }
            $db->transactionCommit();
            $done++;
            out(sprintf('  写入 %d (%s) title=%s', $firstId, 'c' . $p['primary'] . '-' . $oid . ($p['copies'] ? ' +' . count($p['copies']) . '副本' : ''), mb_substr($p['title'], 0, 30)));
        } catch (Throwable $e) {
            $db->transactionRollback();
            $fail++;
            out("  [error] $oid 写库失败已回滚: " . $e->getMessage());
            out("  [abort] 停止后续文章(已成功的保留, 修复后重跑续传)");
            break;
        }
    }

    /* 9. 写后校验(alias 唯一 / lft-rgt 成对 / asset 对应 / workflow 存在) */
    out("--- 写后校验 ---");
    $bad = 0;
    foreach ($plans as $p) {
        $oid = $p['oid'];
        $alias = 'c' . $p['primary'] . '-' . $oid;
        $r = $db->setQuery('SELECT c.id, c.asset_id, a.lft, a.rgt, a.name, (SELECT COUNT(*) FROM #__content c2 WHERE c2.alias=' . $db->quote($alias) . ') AS dup'
            . ' FROM #__content c LEFT JOIN #__assets a ON a.id=c.asset_id WHERE c.alias=' . $db->quote($alias))->loadAssoc();
        if (!$r || (int)$r['dup'] !== 1 || !$r['lft'] || (int)$r['rgt'] !== (int)$r['lft'] + 1
            || $r['name'] !== 'com_content.article.' . $r['id']
            || !(int)$db->setQuery('SELECT COUNT(*) FROM #__workflow_associations WHERE item_id=' . (int)$r['id'])->loadResult()) {
            $bad++;
            out("  [FAIL] $oid alias=$alias " . json_encode($r, JSON_UNESCAPED_UNICODE));
        }
    }
    $totC = (int)$db->setQuery('SELECT COUNT(*) FROM #__content')->loadResult();
    $totW = (int)$db->setQuery('SELECT COUNT(*) FROM #__workflow_associations')->loadResult();
    $totR = (int)$db->setQuery('SELECT COUNT(*) FROM #__redirect_links')->loadResult();
    out(sprintf('结果: 成功 %d / 失败 %d / 校验异常 %d; 表行数 content=%d workflow=%d redirect=%d', $done, $fail, $bad, $totC, $totW, $totR));
    out('=== 同步结束 ===');
    exit($fail || $bad ? 1 : 0);
} catch (Throwable $e) {
    out('[fatal] ' . $e->getMessage() . "\n" . $e->getTraceAsString());
    exit(1);
}
