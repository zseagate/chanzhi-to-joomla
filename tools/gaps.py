#!/usr/bin/env python3
"""量测首页/任意页面的垂直留白: 列出顶层块 y 区间与相邻块之间的空隙, 定位异常空白行。"""
import os
import sys
import time
import importlib.util

spec = importlib.util.spec_from_file_location(
    'shot', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'shot.py'))
shot = importlib.util.module_from_spec(spec)
sys.argv = ['shot.py']
spec.loader.exec_module(shot)

url = sys.argv[1] if len(sys.argv) > 1 else 'http://localhost/'
prof = shot.fresh_profile()
drv = shot.build_driver(1440, 1000, prof)
try:
    drv.get(url)
    time.sleep(2.0)
    drv.execute_script('window.scrollTo(0, document.body.scrollHeight)')
    time.sleep(0.8)
    drv.execute_script('window.scrollTo(0, 0)')
    time.sleep(0.5)

    blocks = drv.execute_script("""
      var out=[], seen=new Set();
      function add(el){
        var r=el.getBoundingClientRect();
        if(r.width<100||r.height<8) return;
        var c=getComputedStyle(el);
        if(c.display==='none'||c.visibility==='hidden') return;
        var key=Math.round(r.top)+'_'+Math.round(r.width);
        if(seen.has(key)) return; seen.add(key);
        out.push({tag:el.tagName.toLowerCase()+'.'+(el.className||'').toString().slice(0,40),
                  cls:(el.className||'').toString().slice(0,50),
                  top:Math.round(r.top), h:Math.round(r.height),
                  w:Math.round(r.width),
                  pt:c.paddingTop, pb:c.paddingBottom, mt:c.marginTop, mb:c.marginBottom,
                  bt:c.borderTopWidth, bb:c.borderBottomWidth});
      }
      // 从 body 起按 DOM 顺序遍历所有元素, 只保留"块级容器"候选
      Array.from(document.body.querySelectorAll('*')).forEach(add);
      out.sort(function(a,b){return a.top-b.top||b.h-a.h;});
      return {blocks:out, docH:Math.max(document.body.scrollHeight,document.documentElement.scrollHeight)};
    """)

    bs = blocks['blocks']
    docH = blocks['docH']
    cols = [b for b in bs if b['w'] >= 1000]
    print('URL=%s  文档高=%d  (宽>=1000 主列块 %d 个)' % (url, docH, len(cols)))
    print('\n--- 主列块(按 y 排序) ---')
    print('%-8s %-8s %-7s %-7s %-6s' % ('top', 'end', 'h', 'pt/pb', 'mt/mb'))
    for b in cols:
        end = b['top'] + b['h']
        print('%-8d %-8d %-7d %-7s %-6s  %s' % (
            b['top'], end, b['h'], '%s/%s' % (b['pt'], b['pb']),
            '%s/%s' % (b['mt'], b['mb']), b['cls'][:60]))

    print('\n--- 相邻块空隙 top20 (宽>1000 主列容器) ---')
    gaps = []
    for a, b in zip(cols, cols[1:]):
        g = b['top'] - (a['top'] + a['h'])
        gaps.append((g, a, b))
    gaps.sort(key=lambda t: -t[0])
    for g, a, b in gaps[:20]:
        print('  gap=%-6d  [%s ...+%d] -> [%s ...+%d]' % (
            g, a['cls'][:40], a['h'], b['cls'][:40], b['h']))
    print('\n--- 文档尾部空白: 最后一个宽>1000 块结束于 %d, 文档高 %d, 尾部留白 %d ---' % (
        max((c['top'] + c['h'] for c in cols), default=0), docH,
        docH - max((c['top'] + c['h'] for c in cols), default=0)))
finally:
    drv.quit()
    import subprocess
    subprocess.run(['rm', '-rf', prof])
