#!/usr/bin/env python3
"""用新备份刷新单页(默认 PAGE_IDS, 本地 id = PAGE_LOCAL_BASE + 源id)。

复用 ETL 的 rewrite_content/附件逻辑。
用法:
  python3 tools/refresh_pages.py                 # 只报告差异, 不写库不拷文件
  python3 tools/refresh_pages.py --apply         # 更新正文 + 补文件
  python3 tools/refresh_pages.py --apply --retire 2007,2008  # 顺带下线源站已删页面
"""
import argparse
import os
import shutil
import subprocess
import sys
from collections import defaultdict

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
from config import (SRC_SQL as NEWSQL, SRC_WEBDATA as NEWDATA, IMAGE_BASE,  # noqa: E402
                    JOOMLA_ROOT, JDB_HOST, JDB_NAME, JDB_USER, JDB_PASS,
                    PAGE_IDS, PAGE_LOCAL_BASE)
IMGROOT = JOOMLA_ROOT + IMAGE_BASE
DB, USER, PWD, HOST = JDB_NAME, JDB_USER, JDB_PASS, JDB_HOST
DOC_EXT = ('doc', 'docx', 'pdf', 'xls', 'xlsx', 'ppt', 'wps', 'txt', 'zip', 'rar')

import chanzhi_to_joomla as E
from dump_table import sql_unescape  # noqa: E402

E.SQL_FILE = NEWSQL
E.DATADIR = NEWDATA


def q(sql):
    return subprocess.run(['mariadb', '-h' + HOST, '-u' + USER, '-p' + PWD, '--batch', DB, '-N', '-e', sql],
                          capture_output=True, text=True).stdout


def build_body(x, files_by_art, ext_lookup):
    img_map, missing, stats = {}, [], {'img_ok': 0, 'img_missing': 0}
    body = E.rewrite_content(x['content'] or '', img_map, missing, ext_lookup, stats)
    atts = [f for f in files_by_art.get(x['id'], [])
            if (f['extension'] or '').lower() in DOC_EXT]
    att_links = []
    for f in atts:
        staged_p, disk = E.resolve_file(f['pathname'], f['extension'], ext_lookup)
        if staged_p is None:
            missing.append('ATT:' + f['pathname'])
            continue
        img_map[staged_p] = disk
        att_links.append((staged_p, f['title'] + '.' + (f['extension'] or '')))
    if att_links:
        lis = ''.join(
            '<li><a href="%s/%s" download="%s">%s</a></li>' % (IMAGE_BASE,
                E.esc(p), E.esc(t), E.esc(t.rsplit('.', 1)[0]))
            for p, t in att_links)
        body += '<h3>附件下载</h3><ul>' + lis + '</ul>'
    return body, img_map, missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--retire', default='', help='源站已删页面的本地 id, 逗号分隔, apply 时置 state=0')
    a = ap.parse_args()

    arts = {x['id']: x for x in E.load('eps_article') if x['id'] in PAGE_IDS}
    files = E.load('eps_file')
    files_by_art = defaultdict(list)
    ext_lookup = {}
    for f in files:
        if f['objectType'] == 'article':
            files_by_art[f['objectID']].append(f)
        ext_lookup.setdefault(f['pathname'], (f['extension'] or '').lower())

    all_img, all_missing = {}, []
    for oldid in PAGE_IDS:
        x = arts[oldid]
        newid = PAGE_LOCAL_BASE + int(oldid)
        body, img_map, missing = build_body(x, files_by_art, ext_lookup)
        all_img.update(img_map)
        all_missing.extend('%s(p%s)' % (m, oldid) for m in missing)
        row = q("SELECT title FROM jos_content WHERE id=%d;" % newid)
        cur = q("SELECT `fulltext` FROM jos_content WHERE id=%d;" % newid)
        # --batch 输出是转义文本(+单行单列, 行尾恰一个 \n 终结符): 还原后再与真实字符比
        row = sql_unescape(row[:-1] if row.endswith('\n') else row)
        cur = sql_unescape(cur[:-1] if cur.endswith('\n') else cur)
        import hashlib
        cur_md5 = hashlib.md5(cur.encode()).hexdigest()
        new_md5 = hashlib.md5(body.encode()).hexdigest()
        print('p%s(本地%d): 标题 %s | 正文 %d字->%d字 | %s' % (
            oldid, newid, '同' if row == x['title'] else '变(%s)' % x['title'][:20],
            len(cur), len(body), '一致' if cur_md5 == new_md5 else '需更新'))
        if a.apply and (cur_md5 != new_md5 or row != x['title']):
            esc = lambda s: "'" + s.replace('\\', '\\\\').replace("'", "\\'") + "'"
            q("UPDATE jos_content SET title=%s, `fulltext`=%s, introtext='', "
              "created=%s, modified=%s WHERE id=%d;"
              % (esc(x['title']), esc(body), esc(x['addedDate']), esc(x['editedDate']), newid))
            print('  已更新本地%d' % newid)

    print('\n文件: 需 %d 个, 缺失 %d 个' % (len(all_img), len(all_missing)))
    for m in all_missing[:20]:
        print('  缺', m)
    copied = 0
    for staged, disk in sorted(all_img.items()):
        dest = IMGROOT + '/' + staged
        if os.path.exists(dest):
            continue
        if a.apply:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(disk, dest)
            os.chmod(dest, 0o644)
            copied += 1
        else:
            print('  待补', staged)
    if a.apply:
        print('已补拷 %d 个文件' % copied)
        if a.retire:
            r = q("UPDATE jos_content SET state=0 WHERE id IN (%s);" % a.retire +
                  "SELECT id,alias,state,LEFT(title,20) FROM jos_content WHERE id IN (%s);" % a.retire)
            print('下线:\n' + r)
    else:
        print('[check] 未写库' + ('; --retire %s 未执行' % a.retire if a.retire else ''))


if __name__ == '__main__':
    main()
