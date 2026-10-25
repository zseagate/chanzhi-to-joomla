#!/usr/bin/env python3
"""老站备份 → 离线增量同步包(零访问老站)。

背景:
  割接后老站可能限流/封 IP, 或同步服务器在境外连不上老站——
  HTTP 抓取式同步(sync_source.php 默认模式)不可用时走这条路:
  拿到老站新备份(mysqldump + 文件包)后离线生成增量包,
  传到目标站点由 `php cli/sync_source.php --bundle=<目录>` 导入。

主栏目口径(多目标站收敛一致):
  - 已在目标库的文章: 沿用目标库主/副本栏目(主行=最小 content id 那行),
    并把备份 relation 新出现的栏目并入副本
  - 全新文章: eps_relation 首个存活栏目(死栏目按 config.DEADMAP 并入;
    纯备份推不出老站 CMS 的规范链接, 首行规则实测约 7/10 吻合, 差异可接受)

bundle 比抓取拿到的信息更全: 置顶(sticky→featured)、keywords、摘要、
附件表(eps_file)都能带上。

用法:
  python3 tools/sync_bundle_from_backup.py --check   # 只打印计划
  python3 tools/sync_bundle_from_backup.py           # 生成 out/sync_bundle/
  可选: --sql= --datadir= --out= --only=<老id,...>

前置: tools/config.py 已填(SRC_SQL/SRC_WEBDATA/JDB_*/ALIVE_ORDER/JCAT_BASE/DEADMAP/SOURCE_HOST)。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (SRC_SQL, SRC_WEBDATA, OUT, JDB_HOST, JDB_NAME, JDB_USER, JDB_PASS,  # noqa: E402
                    ALIVE_ORDER, JCAT_BASE, CAT_NAMES, DEADMAP, SOURCE_HOST, IMAGE_BASE)
from dump_table import split_tuples, sql_unescape  # noqa: E402

CATID_OF = {c: JCAT_BASE + i for i, c in enumerate(ALIVE_ORDER)}
DOC_EXT = {'doc', 'docx', 'pdf', 'xls', 'xlsx', 'ppt', 'wps', 'txt', 'zip', 'rar'}
KNOWN_EXT = {'jpg', 'jpeg', 'png', 'gif', 'swf', 'doc', 'docx', 'pdf', 'xls', 'xlsx',
             'ppt', 'wps', 'txt', 'zip', 'rar'}
FILE_RE = re.compile(r'/file\.php\?f=([^&"\'\s<>]+)([^"\'\s<>]*)')


def q(sql):
    cmd = ['mysql', '-h' + JDB_HOST, '-u' + JDB_USER, '-p' + JDB_PASS, '--batch', JDB_NAME, '-N', '-e', sql]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit('目标库查询失败: ' + r.stderr[:300])
    return r.stdout


def scan_table(sql_path, table, row_filter=None):
    """单遍流式抽某表的全部行(列名来自 CREATE TABLE), 返回 dict 列表。"""
    cols, stmt, grab, out = [], [], False, []
    with open(sql_path, encoding='utf-8', errors='replace') as f:
        for line in f:
            if not cols and ('CREATE TABLE `%s`' % table) in line:
                buf = [line]
                for l2 in f:
                    buf.append(l2)
                    if l2.startswith(')'):
                        break
                cols = re.findall(r'^\s*`(\w+)`', ''.join(buf), re.M)
                continue
            if line.startswith('INSERT INTO `%s`' % table):
                grab, stmt = True, [line]
            elif grab:
                stmt.append(line)
            if grab and line.rstrip().endswith(';'):
                grab = False
                for tup in split_tuples(''.join(stmt).split('VALUES', 1)[1]):
                    row = {k: sql_unescape(v) for k, v in zip(cols, tup)}
                    if row_filter is None or row_filter(row):
                        out.append(row)
    return out


def resolve_file(fp, tparam, ext_lookup, datadir):
    """在文件包 data 目录(及 upload/ 子目录)按后缀候选找文件, 返回 (staged相对路径, 磁盘绝对路径)。"""
    base, ext = os.path.splitext(fp)
    cands = [fp]
    if ext.lower().lstrip('.') in KNOWN_EXT | {'webp'}:
        cands.append(base)
    t = (tparam or '').lower()
    if t:
        cands += [fp + '.' + t, base + '.' + t]
    e2 = (ext_lookup.get(fp) or '').lower()
    if e2:
        cands += [fp + '.' + e2, base + '.' + e2]
    cands += [fp + '.jpg', base + '.jpg']
    for rel in cands:
        for sub in ('', 'upload/'):
            p = os.path.join(datadir, sub + rel)
            if os.path.isfile(p):
                return sub + rel, p
    return None, None


def rewrite_body(html, ext_lookup, datadir, copies_map, missing, fallback):
    """file.php 内链 → IMAGE_BASE/<staged>(staged 自带 upload/ 等子目录前缀);
    fallback 记老站原始URL(导入端缺文件时回退)。"""
    def rep(m):
        fp = m.group(1)
        tail = m.group(2).replace('&amp;', '&')
        tm = re.search(r'[?&]t=([a-zA-Z0-9]+)', tail)
        staged, disk = resolve_file(fp, tm.group(1) if tm else '', ext_lookup, datadir)
        if staged is None:
            missing.append(fp)
            return m.group(0)
        copies_map[staged] = disk
        if SOURCE_HOST:
            fallback[staged] = SOURCE_HOST.rstrip('/') + '/file.php?f=' + fp + tail
        return IMAGE_BASE.rstrip('/') + '/' + staged
    return FILE_RE.sub(rep, html or '')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true', help='只打印计划, 不落盘')
    ap.add_argument('--sql', default=SRC_SQL, help='老站 mysqldump(config.SRC_SQL)')
    ap.add_argument('--datadir', default=SRC_WEBDATA, help='老站 data 目录(config.SRC_WEBDATA)')
    ap.add_argument('--out', default=os.path.join(OUT, 'sync_bundle'), help='bundle 输出目录')
    ap.add_argument('--only', default='', help='只处理指定老文章 id(逗号分隔, 测试用)')
    a = ap.parse_args()

    if not os.path.isfile(a.sql):
        sys.exit('找不到老站 SQL 备份: %s (填 config.SRC_SQL 或 --sql=)' % a.sql)
    if not os.path.isdir(a.datadir):
        sys.exit('data 目录不存在: %s (填 config.SRC_WEBDATA 或 --datadir=)' % a.datadir)
    only = {s for s in a.only.split(',') if s}
    print('SQL :', a.sql)
    print('DATA:', a.datadir)

    # 1. 目标库基线: 老id -> {jcat: content_id}
    known = {}
    for line in q("SELECT id, alias FROM jos_content WHERE alias REGEXP '^c[0-9]+-[0-9]+$'").splitlines():
        parts = line.split('\t')
        m = re.match(r'c(\d+)-(\d+)$', parts[1])
        if m:
            known.setdefault(m.group(2), {})[m.group(1)] = int(parts[0])
    print('目标库已有 oldid:', len(known))

    # 2. 备份里的文章(只留 article 型且不在目标库的, 或 --only 指定)
    arts = scan_table(a.sql, 'eps_article',
                      row_filter=lambda r: r['type'] == 'article'
                      and (r['id'] in only or r['id'] not in known))
    ids = {x['id'] for x in arts}
    print('备份中待同步文章:', len(ids), sorted(ids, key=int)[:20], '...' if len(ids) > 20 else '')

    # 3. relation / eps_file
    rels = scan_table(a.sql, 'eps_relation', row_filter=lambda r: r['type'] == 'article' and r['id'] in ids)
    byart = {}
    for r in rels:
        byart.setdefault(r['id'], []).append(r['category'])
    files = scan_table(a.sql, 'eps_file', row_filter=lambda r: r.get('objectID', '') in ids)
    files_by_art, ext_lookup = {}, {}
    for f in files:
        if (f.get('objectType') or 'article') == 'article':
            files_by_art.setdefault(f.get('objectID'), []).append(f)
        ext_lookup.setdefault(f.get('pathname', ''), (f.get('extension') or '').lower())

    # 4. 逐篇生成
    records, missing = [], []
    for x in sorted(arts, key=lambda r: int(r['id'])):
        oid = x['id']
        cats = []
        for c in byart.get(oid, []):
            j = CATID_OF.get(c) or CATID_OF.get(DEADMAP.get(c, ''))
            if j and j not in cats:
                cats.append(j)
        if oid in known:
            local_jcats = {int(j): i for j, i in known[oid].items()}
            # 目标库主行=最小 content id 那行的 jcat
            primary = min(local_jcats, key=lambda j: local_jcats[j])
            allc = list(dict.fromkeys([primary] + cats + list(local_jcats)))
        else:
            if not cats:
                print('  [warn] %s 无任何存活栏目, 跳过' % oid)
                continue
            primary = cats[0]
            allc = cats
        copies = [c for c in allc if c != primary]

        ext_url = (x.get('link') or '').strip()
        feat = 1 if (x.get('sticky') or '0') not in ('0', '', None) else 0
        copies_map, fallback = {}, {}
        if ext_url:
            body = ''
        else:
            body = rewrite_body(x.get('content') or '', ext_lookup, a.datadir, copies_map, missing, fallback)
            # 文档附件
            atts = [f for f in files_by_art.get(oid, [])
                    if (f.get('extension') or '').lower() in DOC_EXT]
            lis = []
            for f in atts:
                staged, disk = resolve_file(f.get('pathname', ''), f.get('extension', ''),
                                            ext_lookup, a.datadir)
                if staged is None:
                    missing.append('ATT:' + str(f.get('pathname')))
                    continue
                copies_map[staged] = disk
                if SOURCE_HOST:
                    fallback[staged] = '%s/file-download-%s-left.html' % (SOURCE_HOST.rstrip('/'), f.get('id', ''))
                title = (f.get('title') or '附件') + '.' + f.get('extension', '')
                lis.append('<li><a href="%s/%s" download="%s">%s</a></li>'
                           % (IMAGE_BASE.rstrip('/'), staged, title, title.rsplit('.', 1)[0]))
            if lis:
                body += '<h3>附件下载</h3><ul>' + ''.join(lis) + '</ul>'
            # 转载来源
            if x.get('source') == 'copied' and x.get('copyURL'):
                body += '<p>原文链接：<a href="%s" target="_blank" rel="noopener">%s</a></p>' % (
                    x['copyURL'], x['source'])

        date = (x.get('addedDate') or '').strip()
        if date in ('', '0000-00-00 00:00:00'):
            date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        rec = {
            'oldid': int(oid),
            'title': x['title'] or ('文章' + oid),
            'primary': primary,
            'copies': copies,
            'ext': bool(ext_url),
            'urla': ext_url,
            'body': body,
            'date': date,
            'author': x.get('author') or x.get('addedBy') or 'admin',
            'views': int(x.get('views') or 0),
            'featured': feat,
            'metakey': x.get('keywords') or '',
            'metadesc': (x.get('summary') or '')[:120],
            'files': sorted(copies_map),
            'fallback': fallback,
        }
        records.append(rec)
        print('  %s | %s%s | 主%s 副%s | %s | 文件%d | %s'
              % (oid, '[外链] ' if ext_url else '', (x['title'] or '')[:36],
                 primary, copies, date, len(copies_map), ext_url or ''))

    if missing:
        print('--- 未解析文件(%d, 正文保留原URL) ---' % len(missing))
        for m2 in dict.fromkeys(missing):
            print('  ', m2)
    if a.check or not records:
        print('[check] 共 %d 篇, 未落盘' % len(records))
        return

    # 5. 落盘 bundle(staged 路径即 data 目录下的相对路径, 直接拷贝)
    out = a.out
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out + '/files', exist_ok=True)
    nfile = 0
    for rec in records:
        for staged in rec['files']:
            dst = os.path.join(out, 'files', staged)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            src = os.path.join(a.datadir, staged)
            if os.path.isfile(src):
                shutil.copy2(src, dst)
                nfile += 1
            else:
                print('  [warn] bundle 缺文件: %s' % staged)
    meta = {
        'generated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'sql': os.path.basename(a.sql),
        'records': records,
    }
    with open(out + '/bundle.json', 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    print('落盘: %s (bundle.json, %d 篇, %d 个文件)' % (out, len(records), nfile))
    print('目标站导入: php cli/sync_source.php --bundle=<解包后的目录> [--apply]')
    print('打包传站: tar -czf bundle.tgz -C %s .' % out)


if __name__ == '__main__':
    main()
