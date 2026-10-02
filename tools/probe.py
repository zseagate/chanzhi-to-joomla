#!/usr/bin/env python3
"""按 y 区间列出页面上所有可见块级元素的几何, 用于对照源站/新站结构。
用法: python3 probe.py <url> <y0> <y1> [--minw 60]
"""
import importlib.util
import os
import subprocess
import os
import sys
import time

argv = sys.argv[:]          # 必须在加载 shot 前保存(shot 模块会改写 sys.argv)

spec = importlib.util.spec_from_file_location(
    'shot', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'shot.py'))
shot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shot)

url, y0, y1 = argv[1], float(argv[2]), float(argv[3])
minw = float(argv[argv.index('--minw') + 1]) if '--minw' in argv else 60

prof = shot.fresh_profile()
drv = shot.build_driver(1440, 1000, prof)
try:
    drv.get(url)
    time.sleep(2.0)
    drv.execute_script('window.scrollTo(0, document.body.scrollHeight)')
    time.sleep(0.8)
    drv.execute_script('window.scrollTo(0, 0)')
    time.sleep(0.6)
    els = drv.execute_script("""
      var Y0=arguments[0], Y1=arguments[1], MW=arguments[2];
      var o=[],seen=new Set();
      Array.from(document.querySelectorAll('*')).forEach(function(e){
        var r=e.getBoundingClientRect();
        var c=getComputedStyle(e);
        if(c.display==='none'||c.visibility==='hidden') return;
        if(r.top<Y0||r.top>=Y1||r.width<MW||r.height<=10) return;
        var k=Math.round(r.left)+'_'+Math.round(r.top)+'_'+Math.round(r.width);
        if(seen.has(k)) return; seen.add(k);
        o.push({t:e.tagName.toLowerCase(),c:(e.className||'').toString().slice(0,44),
                x:Math.round(r.left),y:Math.round(r.top),w:Math.round(r.width),
                h:Math.round(r.height),pt:c.paddingTop,pb:c.paddingBottom});
      });
      o.sort(function(a,b){return a.y-b.y||a.x-b.x;}); return o;""", y0, y1, minw)
    print('%-11s %-7s %-6s %-6s %-6s  %s' % ('tag', 'x', 'y', 'w', 'h', 'class'))
    for e in els:
        print('%-11s %-7d %-6d %-6d %-6d  %s' % (
            e['t'], e['x'], e['y'], e['w'], e['h'], e['c']))
finally:
    drv.quit()
    subprocess.run(['rm', '-rf', prof])
