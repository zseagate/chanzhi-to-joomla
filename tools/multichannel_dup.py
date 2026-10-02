#!/usr/bin/env python3
"""为源站"交叉发布"补建副本行。

背景:
  老 CMS 允许一篇文章同时挂在多个栏目下(共用同一个 URL,
  各栏目列表页都会列出它)。迁移 ETL 若只取其中一个栏目,
  其余栏目的列表就缺了这篇文章。

依据: eps_relation 给出每篇文章的权威栏目归属。
做法: 已有行保留; 其余存活栏目各补一行副本(同内容, catid/alias 不同)。
  副本 URL 与原站无对应关系, 因此不写 301, 也不新增 sitemap 旧链接。

前置: 先填好 tools/config.py 的 ALIVE_ORDER / JCAT_BASE / CAT_NAMES。
用法:
  python3 tools/multichannel_dup.py            # 只生成 SQL, 不写库
  python3 tools/multichannel_dup.py --apply    # 生成并写入
"""
import argparse
import collections
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (SRC_SQL as SQL_FILE, OUT, JDB_HOST, JDB_NAME, JDB_USER, JDB_PASS,  # noqa: E402
                    ALIVE_ORDER as ALIVE, JCAT_BASE, CAT_NAMES as CN)

DB, USER, PWD = JDB_NAME, JDB_USER, JDB_PASS
HOST = JDB_HOST
HERE = os.path.dirname(os.path.abspath(__file__))

CATJ = {c: JCAT_BASE + i for i, c in enumerate(ALIVE)}
CONTENT_COLS = ['id', 'asset_id', 'title', 'alias', 'introtext', 'fulltext', 'state',
                'catid', 'created', 'created_by', 'created_by_alias', 'modified',
                'modified_by', 'publish_up', 'publish_down', 'images', 'urls',
                'attribs', 'ordering', 'metakey', 'metadesc', 'access', 'hits',
                'metadata', 'featured', 'language']
# 需要反引号的保留字
BK = {'fulltext', 'mod'}


def esc(s):
    # 值来自 mariadb --batch 输出, 本身已是合法转义(\\ \' \n \t, NULL 为 \N 已在上游还原),
    # 此处只加引号, 不得二次转义反斜杠, 否则 \' 会被破坏成 \\\' 导致字符串提前终止。
    if s is None:
        return 'NULL'
    return "'" + s.replace('\r', '') + "'"


def col(c):
    return '`%s`' % c if c in BK else c


def q(sql):
    return subprocess.run(['mariadb', '-h' + HOST, '-u' + USER, '-p' + PWD, '--batch', DB, '-N', '-e', sql],
                          capture_output=True, text=True).stdout


def load_relations():
    """eps_relation(type,id,category) -> {oldid: [存活栏目, 按优先级排序]}"""
    p = subprocess.run([sys.executable, HERE + '/dump_table.py', 'eps_relation', '', SQL_FILE],
                       capture_output=True, text=True)
    rel = collections.defaultdict(set)
    for line in p.stdout.splitlines():
        if line.startswith('#'):
            continue
        f = line.split('\t')
        if len(f) >= 3 and f[0] == 'article' and f[1]:
            rel[f[1]].add(f[2])
    out = {}
    for aid, cats in rel.items():
        alive = sorted([c for c in cats if c in CATJ], key=lambda c: ALIVE.index(c))
        if len(alive) > 1:
            out[aid] = alive
    return out


def build_plan(relations):
    """以本地实际存在行为准, 不预设主栏目(存量按优先级入库, 新10篇按URL slug入库)。
    返回 ({oldid: (模板content_id, [待补 jcat...])}, [本地一行都没有的oldid])。"""
    aids = sorted(relations, key=int)
    have = collections.defaultdict(dict)  # oldid -> {jcat: content_id}
    for i in range(0, len(aids), 200):
        chunk = aids[i:i + 200]
        rx = '^c[0-9]+-(%s)$' % '|'.join(chunk)
        for line in q("SELECT id,alias,catid FROM jos_content "
                      "WHERE alias REGEXP '%s';" % rx).splitlines():
            p = line.split('\t')
            if len(p) == 3 and p[1].startswith('c'):
                try:
                    jc, a = p[1][1:].split('-')
                    if a in relations:
                        d = have[a]
                        if int(jc) not in d or int(p[0]) < d[int(jc)]:
                            d[int(jc)] = int(p[0])
                except ValueError:
                    pass
    plan, missing = {}, []
    for aid, cs in relations.items():
        want = {CATJ[c] for c in cs}
        got = have.get(aid, {})
        if not got:
            missing.append(aid)
            continue
        tmpl = min(got.values())
        todo = sorted(want - set(got))
        if todo:
            plan[aid] = (tmpl, todo)
    return plan, missing


def fetch_canonical(ids):
    cols = ','.join(col(c) for c in CONTENT_COLS)
    res = {}
    for line in q("SELECT %s FROM jos_content WHERE id IN (%s);"
                  % (cols, ','.join(map(str, ids)))).splitlines():
        p = line.split('\t')
        if len(p) != len(CONTENT_COLS):
            continue
        # --batch 模式下 NULL 输出为字面量 NULL(列间为空则是空串), 必须还原
        # (注意 \N 是 INTO OUTFILE 的格式, 客户端 batch 用的是 NULL)
        res[int(p[0])] = dict(zip(CONTENT_COLS,
                                  [None if v in ('NULL', '\\N') else v for v in p]))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()

    relations = load_relations()
    plan, missing = build_plan(relations)
    canon_ids = [v[0] for v in plan.values()]
    canon = fetch_canonical(set(canon_ids))

    n_next = int(q("SELECT COALESCE(MAX(id),0) FROM jos_content;").strip()) + 1
    a_next = int(q("SELECT COALESCE(MAX(id),0) FROM jos_assets;").strip()) + 1
    l_next = int(q("SELECT COALESCE(MAX(lft),0) FROM jos_assets;").strip()) + 1

    ins_c, ins_a = [], []
    agg = collections.Counter()
    for aid, (cid, others) in sorted(plan.items(), key=lambda kv: int(kv[0])):
        base = canon.get(cid)
        if not base:
            continue
        for jc in others:
            agg[jc] += 1
            nid = n_next
            row = dict(base)
            row['id'] = str(nid)
            row['asset_id'] = str(a_next)
            row['catid'] = str(jc)
            row['alias'] = 'c%d-%s' % (jc, aid)
            row['ordering'] = '0'
            row['featured'] = '0'
            row['created_by_alias'] = row.get('created_by_alias') or ''
            ins_c.append('INSERT INTO jos_content (%s) VALUES (%s);' % (
                ','.join(col(c) for c in CONTENT_COLS),
                ','.join(esc(row.get(c)) for c in CONTENT_COLS)))
            ins_a.append("INSERT INTO jos_assets (id,parent_id,lft,rgt,level,name,title,rules) "
                         "VALUES (%d,8,%d,%d,3,'com_content.article.%d',%s,'');" % (
                             a_next, l_next, l_next + 1, nid, esc(row['title'])))
            n_next += 1
            a_next += 1
            l_next += 2

    path = OUT + '/multichannel_dup.sql'
    with open(path, 'w', encoding='utf-8') as fp:
        fp.write("-- 交叉发布副本补建: %d 篇 -> %d 行\n" % (len(plan), len(ins_c)))
        fp.write("-- content %d..%d, assets %d..%d\n" % (
            n_next - len(ins_c), n_next - 1, a_next - len(ins_a), a_next - 1))
        fp.write('\n'.join(ins_a + ins_c))

    print('交叉发布文章 %d 篇, 需补副本 %d 行' % (len(plan), len(ins_c)))
    for jc in sorted(agg):
        print('  %-10s (%d)  +%-4d' % (CN[[c for c, v in CATJ.items() if v == jc][0]], jc, agg[jc]))
    if missing:
        print('本地无对应行的 oldid(%d): %s' % (len(missing), ','.join(sorted(missing, key=int))))
    print('SQL -> %s (%d 字节)' % (path, os.path.getsize(path)))
    if not a.apply:
        print('[check] 未写库')
        return

    r = subprocess.run(['mariadb', '-h' + HOST, '-u' + USER, '-p' + PWD, DB],
                       input=open(path, encoding='utf-8').read(),
                       capture_output=True, text=True)
    print('写库返回码 %d %s' % (r.returncode, r.stderr[:300] if r.stderr else ''))


if __name__ == '__main__':
    main()
