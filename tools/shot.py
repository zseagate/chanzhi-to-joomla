#!/usr/bin/env python3
"""
本地免截图调试工具: 驱动 Firefox headless, 一次给出 截图 + 元素几何 + 资源失败清单。
免去"改代码 -> 让用户截图 -> 再看"的往返。

用法:
  python3 build/tools/shot.py <url>                      # 整页截图, 存 build/out/shots/
  python3 build/tools/shot.py <url> --w 1440 --h 1000    # 指定窗口
  python3 build/tools/shot.py <url> --metrics            # 追加打印关键元素几何/可见性
  python3 build/tools/shot.py <url> --sel ".a,.b"        # 指定要量测的选择器
  python3 build/tools/shot.py <url> --txt out.html       # 同时落盘渲染后的 DOM
  python3 build/tools/shot.py <url> --no-full            # 只截首屏不整页

依赖: firefox + geckodriver($GECKODRIVER) + pip install selenium pillow
说明: Firefox headless 必须先存在 profile 目录(含 prefs.js), 否则报
      "Could not find profile folder", 本脚本自动兜底。
"""
import argparse
import os
import re
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    from config import OUT as _OUT, GECKODRIVER as _GDK, FIREFOX_BIN as _FF
except ImportError:
    _OUT, _GDK, _FF = None, None, None
OUT = os.environ.get('SHOT_OUT') or (_OUT + '/shots' if _OUT else HERE + '/../out/shots')
GDK = os.environ.get('GECKODRIVER') or _GDK or '/usr/local/bin/geckodriver'
FFBIN = os.environ.get('FIREFOX_BIN') or _FF or '/usr/bin/firefox'
WIDE = 1440          # 桌面站默认视口宽
PAD = 40            # 截图时的额外高度余量(过大会在底部造成大片白, 误判为页面留白)


def fresh_profile():
    """Firefox headless 的坑: profile 目录必须先存在且含 prefs.js。"""
    os.makedirs(os.path.expanduser('~/.mozilla/firefox'), exist_ok=True)
    ini = os.path.expanduser('~/.mozilla/firefox/profile.ini')
    if not os.path.exists(ini):
        open(ini, 'w').write('[InstallSafeMode]\n')
    d = '/tmp/opencode/ffprof.' + uuid.uuid4().hex[:8]
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, 'prefs.js'), 'w').write(
        'user_pref("network.proxy.type", 0);\n'
        'user_pref("media.autoplay.default", 0);\n')
    return d


def build_driver(w, h, profile):
    from selenium import webdriver
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.firefox.service import Service
    o = Options()
    o.add_argument('--headless')
    o.add_argument('--width=%d' % w)
    o.add_argument('--height=%d' % h)
    o.add_argument('--disable-popup-blocking')
    o.binary_location = FFBIN
    o.profile = webdriver.FirefoxProfile(profile)
    sv = Service(executable_path=GDK)
    return webdriver.Firefox(service=sv, options=o)


# 量测重点: 首页栅格行距 / 轮播盒 / 正文图片, 以及全站通用容器
DEFAULT_SEL = [
    'body', '.container', '.site-grid', '.grid-child',
    '.container-banner', '.banner', '.banner-slider', '.carousel',
    '.page-body', '.container-article', '.item-page', '.article-content',
    '.list', '.mod-articles-category', 'nav.menu', '.menu',
]
# 自定义容器: 按你的模板追加类名, 或用 --sel 指定
EXTRA_SEL = [
    '.pager', 'article.item-page',
]


def geometry(driver, sels):
    out = []
    for s in sels:
        try:
            els = driver.find_elements('css selector', s)
        except Exception:
            continue
        for i, el in enumerate(els[:3]):
            try:
                r = el.rect
                vis = el.is_displayed()
                st = driver.execute_script(
                    "var e=arguments[0],c=getComputedStyle(e);"
                    "return {h:c.height,pt:c.paddingTop,pb:c.paddingBottom,"
                    "mt:c.marginTop,mb:c.marginBottom,mh:c.margin,pb:c.paddingBottom,"
                    "lh:c.lineHeight,ff:c.fontFamily};", el)
                out.append(dict(sel=s, i=i, x=int(r['x']), y=int(r['y']),
                                w=int(r['width']), h=int(r['height']),
                                displayed=vis, css=st))
            except Exception as e:
                out.append(dict(sel=s, i=i, err=str(e)[:60]))
    return out


def broken_assets(driver):
    """列出未加载成功的图片/资源 —— 用户报"图片加载异常"时无需截图即可定位。"""
    js = """return Array.from(document.images).map(function(e){
      return {src:e.currentSrc||e.getAttribute('src'), natW:e.naturalWidth,
              complete:e.complete, w:e.width, h:e.height};});"""
    imgs = driver.execute_script(js) or []
    bad = []
    for im in imgs:
        src = im.get('src') or ''
        if not src or src.startswith('data:'):
            continue
        if im['complete'] and im['natW'] == 0:
            bad.append('IMG-FAIL  ' + src)
        elif not im['complete']:
            bad.append('IMG-LAID  ' + src)
    return imgs, bad


def _crop(src, top, bot):
    """裁出 y=[top,bot) 区间另存, 便于近距离查看某段留白。"""
    from PIL import Image
    im = Image.open(src)
    box = (0, max(0, top), im.width, min(im.height, bot))
    dst = src[:-4] + '-crop%d_%d.png' % (box[1], box[3])
    im.crop(box).save(dst)
    print('CROP     %s' % dst)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('url')
    ap.add_argument('--out')
    ap.add_argument('--w', type=int, default=WIDE)
    ap.add_argument('--h', type=int, default=1000)
    ap.add_argument('--crop', default='', help='裁切 y 区间 "顶,底", 用于近看局部留白')
    ap.add_argument('--metrics', action='store_true')
    ap.add_argument('--sel', default='')
    ap.add_argument('--txt')
    ap.add_argument('--no-full', action='store_true')
    ap.add_argument('--wait', type=float, default=2.5)
    a = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    prof = fresh_profile()
    drv = build_driver(a.w, a.h, prof)
    try:
        drv.implicitly_wait(5)
        drv.get(a.url)
        time.sleep(a.wait)
        # 滚动到底触发懒加载, 再回顶
        drv.execute_script('window.scrollTo(0, document.body.scrollHeight)')
        time.sleep(0.8)
        drv.execute_script('window.scrollTo(0, 0)')
        time.sleep(0.5)

        title = drv.title
        doc_h = drv.execute_script('return Math.max(document.body.scrollHeight,'
                                   'document.documentElement.scrollHeight)')
        imgs, bad = broken_assets(drv)

        sels = [x.strip() for x in a.sel.split(',') if x.strip()] \
            if a.sel else (DEFAULT_SEL + EXTRA_SEL)
        geo = geometry(drv, sels)

        full = a.h if a.no_full else min(doc_h + PAD, 12000)
        drv.set_window_size(a.w, full)
        time.sleep(1.0)
        path = a.out or os.path.join(
            OUT, time.strftime('%Y%m%d-%H%M%S-') + re.sub(r'[^a-z0-9]+', '-', a.url)[:48] + '.png')
        drv.save_screenshot(path)
        if a.crop:
            top, bot = [int(x) for x in a.crop.split(',')]
            _crop(path, top, bot)
        if a.txt:
            open(a.txt, 'w', encoding='utf-8').write(drv.page_source)

        print('URL      %s' % a.url)
        print('TITLE    %s' % title)
        print('SIZE     %dx%d (整页高 %d)' % (a.w, full, doc_h))
        print('SCREEN   %s' % path)
        print('IMG      %d 张, 其中异常 %d 张' % (len(imgs), len(bad)))
        for b in bad:
            print('  ! %s' % b)

        if a.metrics:
            print('--- 元素几何 ---')
            print('%-42s %-4s %-6s %-5s %-6s %-9s %-8s %-8s' % (
                'selector', 'i', 'x', 'y', 'w', 'h', 'pt/pb', 'mt/mb'))
            for g in geo:
                if 'err' in g:
                    print('%-42s %-4s ERR %s' % (g['sel'][:42], g.get('i'), g['err']))
                    continue
                c = g['css'] or {}
                pt, pb = c.get('pt'), c.get('pb')
                mt, mb = c.get('mt'), c.get('mb')
                if pt in (None, '', '0px') and pb in (None, '', '0px') \
                        and mt in (None, '', '0px') and mb in (None, '', '0px'):
                    continue
                print('%-42s %-4s %-6s %-5s %-6s %-9s %-9s %s%s' % (
                    g['sel'][:42], g['i'], g['x'], g['y'], g['w'], g['h'],
                    '%s/%s' % (pt, pb), '%s/%s' % (mt, mb),
                    '' if g['displayed'] else ' [隐藏]'))
            print('%-42s %-4s' % ('(未列出的选择器 padding/margin 均为 0)', ''))
    finally:
        drv.quit()
        subprocess.run(['rm', '-rf', prof])


if __name__ == '__main__':
    main()
