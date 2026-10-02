#!/usr/bin/env python3
"""
按源站页面 HTML 重建已入库文章的正文(修复朴素替换留下的缺陷:
file.php 的 &o=&s=&v= 尾巴导致图片404、正文字面量 \\n 等)。
做法: 从给定的源页面文件抽取 <section class='article-content'>,
      按主 ETL 同款口径改写内链, 校验/补齐图片落盘, 再回写 fulltext。
用法:
  python3 tools/rebuild_bodies.py --pair 3602:1709:/tmp/new_1709.html [--pair ...] [--check]
  (站外文跳过: urls.urla 非空的行正文不动)
"""
import argparse
import os
import re
import subprocess
import sys

import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (JOOMLA_ROOT, IMAGE_BASE, JDB_HOST, JDB_NAME, JDB_USER, JDB_PASS,  # noqa: E402
                    SOURCE_HOST)

DB, USER, PWD, HOST = JDB_NAME, JDB_USER, JDB_PASS, JDB_HOST
IMGR = JOOMLA_ROOT + IMAGE_BASE
# 源站 data 根下图片实际存于 data/upload/, 迁移后落点为 IMAGE_BASE + '/upload/'
UP = 'upload/'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
SRC = 'https://' + (SOURCE_HOST or 'old-site.example.com')

SEC_RE = re.compile(r"<section class=['\"]article-content['\"]>(.*?)</section>", re.S)
FILE_RE = re.compile(r"/file\.php\?f=([^&\"']+)((?:&[^\"']*)?)")
TPL = {'jpg', 'jpeg', 'png', 'gif', 'webp'}


def q(sql):
    return subprocess.run(['mariadb', '-h' + HOST, '-u' + USER, '-p' + PWD, '--batch', DB, '-N', '-e', sql],
                          capture_output=True, text=True).stdout


def extract(sec_html):
    """从源页面取正文; 源站无字面量 \\n, 抽取后原样保留真实换行。"""
    m = SEC_RE.search(sec_html)
    return m.group(1).strip('\n') if m else None


def rewrite(html, need):
    """file.php 内链 → IMAGE_BASE/upload/<fpath>[.ext]; 登记需下载项。"""
    def rep(m):
        fp = m.group(1)
        tail = m.group(2).replace('&amp;', '&')
        tm = re.search(r'[?&]t=([a-zA-Z0-9]+)', tail)
        t = tm.group(1).lower() if tm else ''
        base, ext = os.path.splitext(fp)
        if ext.lower() in TPL:
            path = fp
        else:
            path = fp + ('.' + t if t in TPL else '.jpg')
        staged = UP + path
        if not os.path.exists(IMGR + '/' + staged):
            need.append((staged, fp, tail))
        return IMAGE_BASE + '/' + staged
    return FILE_RE.sub(rep, html)


def fetch(staged, fp, tail):
    """从线上 file.php 下载缺失图片到 IMAGE_BASE/upload/。"""
    url = SRC + '/file.php?f=' + fp + tail
    subprocess.run(['curl', '-sL', '--max-time', '40', '-A', UA, '-o',
                    '/tmp/dl_bin', url])
    if os.path.getsize('/tmp/dl_bin') < 200:
        return False
    os.makedirs(os.path.dirname(IMGR + '/' + staged), exist_ok=True)
    shutil_c = subprocess.run(['cp', '/tmp/dl_bin', IMGR + '/' + staged])
    return shutil_c.returncode == 0


def esc(s):
    return s.replace('\\', '\\\\').replace("'", "\\'")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true', help='只报告差异, 不写库')
    ap.add_argument('--pair', action='append', default=[], help='newid:oldid:源HTML路径, 可多次')
    a = ap.parse_args()
    PAIRS = [tuple(x.split(':')[0:2]) + (':'.join(x.split(':')[2:]),) for x in a.pair]
    PAIRS = [(int(n), int(o), f) for n, o, f in PAIRS]

    if not PAIRS:
        print('请用 --pair newid:oldid:html 指定文章'); return
    rows, need, skip = [], [], []
    for newid, oldid, f in PAIRS:
        html = open(f, encoding='utf-8', errors='replace').read()
        body = extract(html)
        if body is None:
            skip.append((newid, oldid, '未找到 article-content'))
            continue
        body = rewrite(body, need)
        rows.append((newid, oldid, body))

    print('--- 待补齐图片(%d) ---' % len(need))
    for path, fp, tail in need:
        ok = fetch(path, fp, tail)
        print('  %-58s %s' % (path, 'OK' if ok else 'FAIL'))

    print('--- 待更新文章(%d) ---' % len(rows))
    for newid, oldid, body in rows:
        has_nl = '\\n' in body
        has_php = 'file.php' in body
        print('  id=%d old=%d len=%d 字面\\n=%s file.php残留=%s'
              % (newid, oldid, len(body), has_nl, has_php))
    if skip:
        print('--- 跳过 ---')
        for s in skip:
            print('  ', s)

    if a.check:
        print('\n[check] 未写库')
        return

    for newid, oldid, body in rows:
        cur = q("SELECT `fulltext` FROM jos_content WHERE id=%d" % newid)
        sql = "UPDATE jos_content SET `fulltext`='%s' WHERE id=%d;" % (esc(body), newid)
        subprocess.run(['mariadb', '-h' + HOST, '-u' + USER, '-p' + PWD, DB, '-e', sql],
                       capture_output=True, text=True)
        print('  id=%d 写库完成 (原长 %d -> 新长 %d)' % (newid, len(cur), len(body)))


if __name__ == '__main__':
    main()
