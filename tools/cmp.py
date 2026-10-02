#!/usr/bin/env python3
"""同一选择器集在源站/新站的逐项几何对照, 并输出等尺寸左右对比图。
用法: python3 tools/cmp.py <源站URL> <新站URL> [--y0 360 --y1 980] [--sel css1,css2] [--out 对比图.png]
"""
import argparse
import os
import sys
import time
import importlib.util

spec = importlib.util.spec_from_file_location(
    'shot', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'shot.py'))
shot = importlib.util.module_from_spec(spec)
sys.argv = ['shot.py']
spec.loader.exec_module(shot)

DEFAULT_SELS = [
    '.container-nav', '.site-grid', '.banner', '.banner img',
    '.container-top-a', '.container-top-a img',
    '.mod-articles-category',
]


def snap(url):
    prof = shot.fresh_profile()
    drv = shot.build_driver(1440, 1000, prof)
    drv.get(url)
    time.sleep(2.0)
    drv.execute_script('window.scrollTo(0,document.body.scrollHeight)')
    time.sleep(0.8)
    drv.execute_script('window.scrollTo(0,0)')
    time.sleep(0.6)
    drv.set_window_size(1440, min(drv.execute_script(
        'return document.body.scrollHeight') + 60, 12000))
    time.sleep(0.8)
    out = '/tmp/cmp_%s.png' % abs(hash(url))
    drv.save_screenshot(out)
    return out, drv, prof


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src')
    ap.add_argument('dst')
    ap.add_argument('--y0', type=int, default=360)
    ap.add_argument('--y1', type=int, default=980)
    ap.add_argument('--sel', default='')
    ap.add_argument('--out', default='/tmp/cmp_side_by_side.png')
    a = ap.parse_args()
    sels = [x.strip() for x in a.sel.split(',') if x.strip()] or DEFAULT_SELS

    shots = []
    import subprocess
    for name, url in (('源站', a.src), ('新站', a.dst)):
        out, drv, prof = snap(url)
        shots.append(out)
        try:
            print('\n' + '=' * 78)
            print('%s  %s' % (name, url))
            print('-' * 78)
            print('%-28s %-6s %-6s %-7s %-7s %-12s %-8s' % (
                'selector', 'x', 'y', 'w', 'h', 'pt/pb', 'mt/mb'))
            for s in sels:
                try:
                    els = drv.find_elements('css selector', s)
                except Exception:
                    continue
                for i, el in enumerate(els[:2]):
                    try:
                        r = el.rect
                        c = drv.execute_script(
                            "var e=arguments[0],c=getComputedStyle(e);"
                            "return [c.paddingTop,c.paddingBottom,c.marginTop,c.marginBottom,"
                            "c.backgroundImage.slice(0,28)];", el)
                        print('%-28s %-6d %-6d %-7d %-7d %-12s %-8s %s' % (
                            (s[:26] + ('#%d' % i if i else '')),
                            int(r['x']), int(r['y']), int(r['width']), int(r['height']),
                            '%s/%s' % (c[0], c[1]), '%s/%s' % (c[2], c[3]), c[4]))
                    except Exception as e:
                        print('%-28s ERR %s' % (s[:26], str(e)[:40]))
        finally:
            drv.quit()
            subprocess.run(['rm', '-rf', prof])

    from PIL import Image
    x0, y0, x1, y1 = 0, a.y0, 1440, a.y1
    ims = [Image.open(p).crop((x0, y0, x1, y1)) for p in shots]
    W, H = ims[0].width, ims[0].height
    canvas = Image.new('RGB', (W * 2 + 12, H + 22), 'white')
    canvas.paste(ims[0], (0, 22))
    canvas.paste(ims[1], (W + 12, 22))
    canvas = canvas.resize((int(canvas.width * 0.62), int(canvas.height * 0.62)))
    canvas.save(a.out)
    print('\n对比图: %s  (左=源站  右=新站, y=%d..%d)' % (a.out, a.y0, a.y1))


if __name__ == '__main__':
    main()
