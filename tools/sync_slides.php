#!/usr/bin/env php
<?php
/**
 * sync_slides.php — 源站首页轮播(block5 / slide5-73) → 本地 mod_custom 模块同步
 *
 * 背景: sync_source.php 只同步文章; 首页轮播是蝉知的"幻灯区块", 不在 sitemap/文章页里,
 *       所以从迁移那一刻起就再没更新过(2026-10 排查: 本地仍是 2025-09 的 7 张旧图)。
 *
 * 说明: 模块内容里的 sxpg-slides / sxpg-chevron 是配套模板的 CSS 类名,
 *       换模板时把 buildContent() 里的类名一起改掉即可。
 *
 * 部署: 与 sync_source.php 同级站点 cli/ 目录, SSH 执行:
 *   php cli/sync_slides.php                    # dry-run: 打印源站幻灯清单与差异
 *   php cli/sync_slides.php --apply            # 下载图片 + 重建模块内容
 *   可选: --src-base=<relay.php> --relay-token=<token>  (只读中继, 用法同 sync_source)
 *         --dir=/images/site/slides  幻灯图落点(默认见 SLIDE_DIR_DEFAULT)
 *         --module=<id>   指定模块(默认自动找内容含 sxpg-slides 的 mod_custom)
 *         --no-download   只改内容不下载图片(图已在磁盘时)
 *
 * 产出: <--dir>/<原文件名>（如 73_1404.jpg）+ module(content) 重建为
 *       .sxpg-slides > .carousel-inner > .item(.active) + 左右箭头 + 圆点
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

const SRC_BASE_DEFAULT = 'https://old-site.example.com';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36';
// 源站栏目 slug => Joomla catid (与 sync_source.php 一致), 用于把幻灯外链换成本地文章
const CHANNELS = [
    'caijingxinwen' => 1001, 'zhongyaotongzhi' => 1002, 'xinxijianbao' => 1003,
    'kaoshipeixun'  => 1004, 'hangyejianguan' => 1005, 'pingguzhunze' => 1006,
    'zhengcefagui'  => 1007, 'huiyuanfuwu'    => 1008, 'huiyuanguanli' => 1009,
    'hangyedangjian'=> 1010, 'lishihui'       => 1011, 'xiehuidashiji' => 1012,
    'xiazaizhongxin'=> 1013,
];
// 幻灯图落点(相对站点根)。用 --dir 或环境变量 SLIDE_DIR 覆盖
const SLIDE_DIR_DEFAULT = '/images/site/slides';

$opt = getopt('', ['apply', 'src-base:', 'relay-token:', 'proxy:', 'module:', 'no-download', 'timeout:', 'dir:']);
$APPLY = isset($opt['apply']);
$NO_DL = isset($opt['no-download']);
$SRC_BASE = isset($opt['src-base']) ? rtrim((string)$opt['src-base'], '/') : SRC_BASE_DEFAULT;
$RELAY_TOKEN = isset($opt['relay-token']) ? (string)$opt['relay-token']
             : ((string)getenv('RELAY_TOKEN') ?: (string)getenv('SXPG_RELAY_TOKEN'));
$PROXY = isset($opt['proxy']) ? (string)$opt['proxy'] : '';
$TIMEOUT = isset($opt['timeout']) ? max(10, (int)$opt['timeout']) : 60;
$MODULE_ID = isset($opt['module']) ? (int)$opt['module'] : 0;
$SRC_HOST = (string)parse_url($SRC_BASE, PHP_URL_HOST);
$SLIDE_DIR = rtrim((string)($opt['dir'] ?? getenv('SLIDE_DIR') ?: SLIDE_DIR_DEFAULT), '/');
if ($SLIDE_DIR === '' || $SLIDE_DIR[0] !== '/') { fwrite(STDERR, "--dir 必须是以 / 开头的站内路径\n"); exit(1); }

function out(string $m): void { echo '[' . date('H:i:s') . '] ' . $m . "\n"; }

function srcUrl(string $path): string
{
    global $SRC_BASE, $RELAY_TOKEN;
    if (preg_match('#\.php$#', $SRC_BASE)) {
        $sep = strpos($SRC_BASE, '?') === false ? '?' : '&';
        $u = $SRC_BASE . $sep . 'p=' . rawurlencode($path);
        if ($RELAY_TOKEN !== '') {
            $u .= '&t=' . rawurlencode($RELAY_TOKEN);
        }
        return $u;
    }
    return $SRC_BASE . $path;
}

/** @return array{0:int,1:string} */
function fetch(string $url): array
{
    global $PROXY, $TIMEOUT, $RELAY_TOKEN;
    $ch = curl_init($url);
    $o = [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_MAXREDIRS      => 5,
        CURLOPT_TIMEOUT        => $TIMEOUT,
        CURLOPT_CONNECTTIMEOUT => 10,
        CURLOPT_USERAGENT      => UA,
    ];
    if (defined('CURLOPT_IPRESOLVE')) { $o[CURLOPT_IPRESOLVE] = CURL_IPRESOLVE_V4; }
    if ($PROXY !== '') { $o[CURLOPT_PROXY] = $PROXY; }
    if ($RELAY_TOKEN !== '') { $o[CURLOPT_HTTPHEADER] = ['X-Relay-Token: ' . $RELAY_TOKEN]; }
    curl_setopt_array($ch, $o);
    $body = curl_exec($ch);
    $code = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $err  = curl_errno($ch);
    curl_close($ch);
    if ($err !== 0) { out("  [http] errno=$err $url"); return [0, '']; }
    return [$code, (string)$body];
}

/** 解析首页轮播块 → [[oldid, href, target, imgSrc, fileName], ...] */
function parseSlides(string $html): array
{
    if (!preg_match("#<div[^>]*class='carousel slide'[^>]*>(.*?)<ol class=\"carousel-indicators\"#s", $html, $m)) {
        return [];
    }
    $blk = $m[1];
    $items = [];
    if (preg_match_all("#<div data-id='(\d+)' class='item[^']*'\s+data-url='([^']*)'\s+data-target='([^']*)'[^>]*>.*?<img src='([^']+)'#s", $blk, $ms, PREG_SET_ORDER)) {
        foreach ($ms as $x) {
            $src = html_entity_decode($x[4], ENT_QUOTES);
            if (!preg_match('/[?&]f=([^&]+)/', $src, $fm)) {
                continue;
            }
            $items[] = [
                'oldid'  => (int)$x[1],
                'href'   => html_entity_decode($x[2], ENT_QUOTES),
                'target' => $x[3] === '_blank' ? '_blank' : '_self',
                'src'    => $src,
                'file'   => basename($fm[1]),
            ];
        }
    }
    return $items;
}

function buildContent(array $items): string
{
    $h = '<div class="sxpg-slides carousel">' . "\n" . '  <div class="carousel-inner">' . "\n";
    foreach ($items as $i => $s) {
        $cls = 'item' . ($i === 0 ? ' active' : '');
        $h .= '    <div class="' . $cls . '"><a href="' . htmlspecialchars($s['href'], ENT_QUOTES)
            . '" target="' . $s['target'] . '"><img src="' . $SLIDE_DIR . '/' . rawurlencode($s['file'])
            . '" alt="幻灯"></a></div>' . "\n";
    }
    $h .= "  </div>\n";
    $h .= '  <a class="carousel-control left" href="#" role="button" aria-label="上一张"><span class="sxpg-chevron">‹</span></a>' . "\n";
    $h .= '  <a class="carousel-control right" href="#" role="button" aria-label="下一张"><span class="sxpg-chevron">›</span></a>' . "\n";
    $h .= '  <ol class="carousel-indicators">' . "\n";
    foreach ($items as $i => $s) {
        $h .= '    <li' . ($i === 0 ? ' class="active"' : '') . '></li>' . "\n";
    }
    $h .= "  </ol>\n</div>";
    return $h;
}

try {
    $db = Factory::getContainer()->get(DatabaseInterface::class);

    out('=== 轮播同步开始 ' . ($APPLY ? '[APPLY 实写]' : '[dry-run 预览]') . " [src=$SRC_BASE] ===");

    // 1. 找目标模块
    if (!$MODULE_ID) {
        $row = $db->setQuery("SELECT id FROM #__modules WHERE module='mod_custom' AND content LIKE '%sxpg-slides%' ORDER BY position='top-a' DESC, id LIMIT 1")->loadResult();
        $MODULE_ID = (int)$row;
    }
    if (!$MODULE_ID) {
        throw new RuntimeException("未找到轮播模块(内容含 sxpg-slides 的 mod_custom), 可用 --module=<id> 指定");
    }
    $mod = $db->setQuery('SELECT id,title,position,content FROM #__modules WHERE id=' . $MODULE_ID)->loadAssoc();
    if (!$mod) {
        throw new RuntimeException("模块不存在: $MODULE_ID");
    }
    out("目标模块: {$mod['id']} 《{$mod['title']}》 position={$mod['position']}");

    // 2. 抓首页并解析
    [$code, $home] = fetch(srcUrl('/'));
    if ($code !== 200) {
        throw new RuntimeException("首页获取失败 http=$code");
    }
    $items = parseSlides($home);
    if (!$items) {
        throw new RuntimeException('首页未解析到轮播块(源站模板可能改版)');
    }
    out('源站幻灯 ' . count($items) . ' 张: ' . implode(', ', array_column($items, 'file')));

    // 源站文章链接 → 本地文章链接(未迁移的保留源站绝对地址)
    $aliases = [];
    foreach ($items as $s) {
        if (preg_match('#^https?://' . preg_quote($SRC_HOST, '#') . '/([a-z]+)/(\d+)\.html#', $s['href'], $hm)
            && isset(CHANNELS[$hm[1]])) {
            $aliases[] = 'c' . CHANNELS[$hm[1]] . '-' . $hm[2];
        }
    }
    $map = [];
    if ($aliases) {
        foreach ((array)$db->setQuery("SELECT id,alias FROM #__content WHERE alias IN (" . implode(',', array_map([$db, 'quote'], $aliases)) . ")")->loadAssocList() as $r) {
            $map[$r['alias']] = (int)$r['id'];
        }
    }
    foreach ($items as &$s) {
        if (preg_match('#^https?://' . preg_quote($SRC_HOST, '#') . '/([a-z]+)/(\d+)\.html#', $s['href'], $hm)
            && isset(CHANNELS[$hm[1]])) {
            $al = 'c' . CHANNELS[$hm[1]] . '-' . $hm[2];
            if (isset($map[$al])) {
                $s['href'] = '/index.php?option=com_content&view=article&id=' . $map[$al];
            }
        }
    }
    unset($s);

    // 3. 对比本地磁盘
    $dirAbs = JPATH_ROOT . $SLIDE_DIR;
    if (!is_dir($dirAbs)) { @mkdir($dirAbs, 0755, true); }
    foreach ($items as $i => $s) {
        $local = $dirAbs . '/' . $s['file'];
        $items[$i]['exists'] = is_file($local) && filesize($local) > 200;
        out('  ' . ($items[$i]['exists'] ? '[有]' : '[缺]') . ' ' . $s['file'] . ' <- ' . $s['href'] . ' (' . $s['target'] . ')');
    }

    $newContent = buildContent($items);
    $changed = trim((string)$mod['content']) !== $newContent;
    out('模块内容: ' . ($changed ? '需重建' : '已一致'));

    if (!$APPLY) {
        out('=== dry-run 结束(未下载未写库) ===');
        exit(0);
    }

    // 4. 下载缺失图片
    if (!$NO_DL) {
        foreach ($items as $s) {
            $local = $dirAbs . '/' . $s['file'];
            if ($s['exists']) { continue; }
            [$c, $bin] = fetch(srcUrl($s['src']));
            if ($c !== 200 || strlen($bin) < 200) {
                out("  [warn] 下载失败 http=$c {$s['file']} —— 该张保留旧内容时会被引用为坏链");
                continue;
            }
            if (@file_put_contents($local, $bin) === false) {
                out("  [warn] 写入失败 $local");
                continue;
            }
            out('  图片 OK ' . $s['file'] . ' (' . strlen($bin) . 'B)');
        }
    }

    // 5. 写模块内容
    if ($changed) {
        $q = $db->getQuery(true)
            ->update($db->quoteName('#__modules'))
            ->set($db->quoteName('content') . '=' . $db->quote($newContent))
            ->where('id=' . (int)$MODULE_ID);
        $db->setQuery($q)->execute();
        out('模块内容已更新');
    }

    // 6. 清缓存(模块缓存默认 900s; CLI 无 Application, 直接删站点 cache 下相关目录)
    $removed = 0;
    foreach ((array)glob(JPATH_SITE . '/cache/*mod_custom*') as $d) {
        if (is_dir($d)) { foreach ((array)glob($d . '/*') as $f) { @unlink($f); } @rmdir($d); $removed++; }
    }
    foreach ((array)glob(JPATH_SITE . '/cache/*com_modules*') as $d) {
        if (is_dir($d)) { foreach ((array)glob($d . '/*') as $f) { @unlink($f); } @rmdir($d); $removed++; }
    }
    out('缓存清理: ' . $removed . ' 项');

    // 7. 写后校验
    $now = (string)$db->setQuery('SELECT content FROM #__modules WHERE id=' . (int)$MODULE_ID)->loadResult();
    $cnt = preg_match_all('#class="item#', $now);
    $ok  = $cnt === count($items) && strpos($now, 'carousel-indicators') !== false && strpos($now, 'carousel-control') !== false;
    $missing = [];
    foreach ($items as $s) {
        if (!is_file($dirAbs . '/' . $s['file'])) { $missing[] = $s['file']; }
    }
    out('写后校验: 条目 ' . $cnt . '/' . count($items) . ', 控件 ' . (strpos($now, 'carousel-control') !== false ? 'OK' : '缺')
        . ', 缺图 ' . ($missing ? implode(',', $missing) : '无'));
    out($ok && !$missing ? '=== 轮播同步完成 ===' : '=== 完成但有告警 ===');
} catch (Throwable $e) {
    out('[fatal] ' . $e->getMessage());
    exit(1);
}
