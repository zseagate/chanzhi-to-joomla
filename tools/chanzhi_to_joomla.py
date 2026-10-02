#!/usr/bin/env python3
"""
蝉知(ZSITE 8.x, eps_*) → Joomla 5 迁移 ETL
用法:
  python3 tools/chanzhi_to_joomla.py --trial   # 试迁100篇(分层抽样) → out/
  python3 tools/chanzhi_to_joomla.py --full    # 全量(割接用新鲜备份重跑)
前置: 先填好 tools/config.py(源备份路径、栏目映射 ALIVE_ORDER/DEADMAP、分类基址)。
产出: categories.sql / content_*.sql(含301) / map_article.csv / images暂存 / REPORT.md
"""
import argparse
import csv
import json
import os
import re
import shutil
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dump_table import split_tuples, sql_unescape  # noqa: E402
from config import SRC_SQL as SQL_FILE, SRC_WEBDATA as DATADIR, OUT, IMAGE_BASE  # noqa: E402
from config import SOURCE_HOST  # noqa: E402

# ↓↓↓ 适配点: 下面 CAT_BASE/PAGE_CAT/ART_BASE/ALIVE_ORDER/DEADMAP 按你的站改 ↓↓↓


# Joomla 侧固定 ID 段(避开 Joomla 默认小 ID)
CAT_BASE = 1001   # 分类 1001..1013 为13频道,1014 为单页"协会概况"
PAGE_CAT = 1014
ART_BASE = 2001   # 文章自增起点(相对 MAX(id))
FIELD_NAME = 'exturl'

ALIVE_ORDER = ['21', '17', '24', '77', '44', '51', '2', '69', '43', '35', '39', '6', '71']
DEADMAP = {'65': '21', '38': '24', '26': '21', '28': '35', '42': '17', '14': '17',
           '67': '21', '29': '35', '15': '17', '66': '21', '22': '51', '23': '21',
           '13': '39', '10': '39', '11': '39', '12': '77', '27': '24', '72': '2'}

FILE_RE = re.compile(r'/file\.php\?f=([^&"\'\s<>]+)([^"\'\s<>]*)')
KNOWN_EXT = {'jpg', 'jpeg', 'png', 'gif', 'swf', 'doc', 'docx', 'pdf', 'xls', 'xlsx',
             'ppt', 'wps', 'txt', 'zip', 'rar'}


def esc(s):
    if s is None:
        return ''
    return s.replace('\\', '\\\\').replace("'", "\\'").replace('\r', '')


def load(table):
    raw = open(SQL_FILE, encoding='utf-8', errors='replace').read()
    m = re.search(r'CREATE TABLE `%s` \((.*?)\) ENGINE' % table, raw, re.S)
    names = re.findall(r'^\s*`(\w+)`', m.group(1), re.M)
    out = []
    for s in raw.split(';\n'):
        if s.startswith('INSERT INTO `%s`' % table):
            for tup in split_tuples(s.split('VALUES', 1)[1]):
                # SQL反转义: dump中的\n/\"等还原为真实字符(否则正文残留\n字样)
                out.append({k: sql_unescape(v) for k, v in zip(names, tup)})
    return out


def resolve_file(fpath, tparam, ext_lookup):
    """f=xxx 可能带/不带后缀；磁盘多为无后缀存储。
    返回 (暂存相对路径[补后缀], 磁盘绝对路径) 或 (None, None)。"""
    ext_known = (tparam.lower() if tparam and tparam.lower() in KNOWN_EXT else '') \
        or ext_lookup.get(fpath, '')
    cands = [fpath]
    if '.' in fpath.rsplit('/', 1)[-1]:
        base, e = fpath.rsplit('.', 1)
        if e.lower() in KNOWN_EXT:
            cands.append(base)
    if ext_known and '.' not in fpath.rsplit('/', 1)[-1]:
        cands.append(fpath + '.' + ext_known)
    for c in cands:
        if os.path.exists(DATADIR + '/' + c):
            disk = DATADIR + '/' + c
            staged = c if '.' in c.rsplit('/', 1)[-1] else (c + '.' + ext_known if ext_known else c)
            return staged, disk
    for c in cands:
        if os.path.exists(DATADIR + '/upload/' + c):
            disk = DATADIR + '/upload/' + c
            staged = 'upload/' + (c if '.' in c.rsplit('/', 1)[-1] else (c + '.' + ext_known if ext_known else c))
            return staged, disk
    return None, None


def rewrite_content(html, img_map, missing, ext_lookup, stats):
    """改写 file.php 内链 → IMAGE_BASE/xxx；登记缺失文件。"""
    def rep(m):
        fpath = m.group(1).replace('&amp;', '&')
        tm = re.search(r'[?&]t=([a-zA-Z0-9]+)', m.group(2).replace('&amp;', '&'))
        tparam = tm.group(1) if tm else ''
        staged, disk = resolve_file(fpath, tparam, ext_lookup)
        if staged:
            img_map[staged] = disk
            stats['img_ok'] += 1
            return IMAGE_BASE + '/' + staged
        missing.append(fpath)
        stats['img_missing'] += 1
        return m.group(0)
    html = FILE_RE.sub(rep, html)
    if SOURCE_HOST:
        html = html.replace('http://' + SOURCE_HOST, '').replace('https://' + SOURCE_HOST, '')
    return html


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--trial', action='store_true')
    ap.add_argument('--full', action='store_true')
    ap.add_argument('--art-base', type=int, default=2001, help='文章起始ID(须大于目标库现有MAX(id))')
    ap.add_argument('--asset-base', type=int, default=5001, help='文章资产起始ID(须大于目标库assets MAX(id))')
    a = ap.parse_args()
    trial = a.trial and not a.full
    ART0, AST0 = a.art_base, a.asset_base
    os.makedirs(OUT + '/images', exist_ok=True)

    cats = {c['id']: c for c in load('eps_category')}
    arts = [x for x in load('eps_article')]
    rels = load('eps_relation')
    files = load('eps_file')
    byart = defaultdict(list)
    for r in rels:
        if r['type'] == 'article':
            byart[r['id']].append(r['category'])
    files_by_art = defaultdict(list)
    ext_lookup = {}
    for f in files:
        if f['objectType'] == 'article':
            files_by_art[f['objectID']].append(f)
        ext_lookup.setdefault(f['pathname'], (f['extension'] or '').lower())

    catid_of = {}   # 蝉知catid -> Joomla catid
    for i, c in enumerate(ALIVE_ORDER):
        catid_of[c] = CAT_BASE + i
    jcats = [(catid_of[c], cats[c]['name'], cats[c]['alias']) for c in ALIVE_ORDER]
    jcats.append((PAGE_CAT, '协会概况', 'xiehuigaikuang'))
    NCAT = len(jcats)

    # 文章归类: 取存活类(首个按 ALIVE_ORDER 优先级)；孤儿按 DEADMAP
    def pick_cat(aid):
        cs = byart.get(aid, [])
        for c in ALIVE_ORDER:
            if c in cs:
                return c, False
        for c in cs:
            if c in DEADMAP:
                return DEADMAP[c], True
        return None, True

    articles = [x for x in arts if x['type'] == 'article']
    pages = [x for x in arts if x['type'] == 'page']

    if trial:
        # 分层抽样: 全部单页 + 置顶 + 站外link抽10 + 孤儿抽10 + 转载抽5 + 每类约5
        chosen, seen = [], set()

        def add(x):
            if x['id'] not in seen:
                seen.add(x['id'])
                chosen.append(x)
        for x in pages:
            add(x)
        for x in articles:
            if x['sticky'] not in ('0', ''):
                add(x)
        links = [x for x in articles if x['link']]
        orph = [x for x in articles if pick_cat(x['id'])[1] and not x['link']]
        for x in links[:10]:
            add(x)
        for x in orph[:10]:
            add(x)
        cop = [x for x in articles if x['source'] == 'copied' and x['id'] not in seen][:5]
        for x in cop:
            add(x)
        per = defaultdict(int)
        for x in sorted(articles, key=lambda z: int(z['id'])):
            if len(chosen) >= 100:
                break
            c, _ = pick_cat(x['id'])
            if c and per[c] < 5 and x['id'] not in seen:
                per[c] += 1
                add(x)
        work = chosen
    else:
        work = [x for x in articles] + pages

    img_map, missing = {}, []
    stats = {'img_ok': 0, 'img_missing': 0}
    rows, redirects, fvals, amap = [], [], [], []
    link_cnt = orphan_cnt = 0
    for idx, x in enumerate(sorted(work, key=lambda z: (z['type'] != 'page', int(z['id'])))):
        newid = ART0 + idx
        is_page = x['type'] == 'page'
        if is_page:
            jcat = PAGE_CAT
        else:
            c, is_orph = pick_cat(x['id'])
            if c is None:
                continue
            jcat = catid_of[c]
            orphan_cnt += is_orph
        body = rewrite_content(x['content'] or '', img_map, missing, ext_lookup, stats)
        # 附件下载列表(文档类): 经 resolve_file 还原后缀后落点
        atts = [f for f in files_by_art.get(x['id'], [])
                if f['extension'].lower() in ('doc', 'docx', 'pdf', 'xls', 'xlsx', 'ppt', 'wps', 'txt', 'zip', 'rar')]
        att_links = []
        for f in atts:
            staged_p, disk = resolve_file(f['pathname'], f['extension'], ext_lookup)
            if staged_p is None:
                missing.append('ATT:' + f['pathname'])
                continue
            img_map[staged_p] = disk
            att_links.append((staged_p, f['title'] + '.' + f['extension']))
        if att_links:
            lis = ''.join(
                '<li><a href="%s/%s" download="%s">%s</a></li>' % (IMAGE_BASE,
                    esc(p), esc(t), esc(t.rsplit('.', 1)[0]))
                for p, t in att_links)
            body += '<h3>附件下载</h3><ul>' + lis + '</ul>'
        summary = x['summary'] or ''
        desc120 = summary[:120]
        ext = x['link'] or ''
        if ext:
            link_cnt += 1
        urls = ''
        if ext:
            # 站外文: 原生"链接A"直跳(模板覆盖标题链接); 转载来源同时保留正文末
            urls = json.dumps({"urla": ext, "urlatext": x['title'], "targeta": 1}, ensure_ascii=False)
            if x['source'] == 'copied' and x['copyURL']:
                body += '<p>原文链接：<a href="%s" target="_blank" rel="noopener">%s</a></p>' % (
                    esc(x['copyURL']), esc(x['source']))
        elif x['source'] == 'copied' and x['copyURL']:
            urls = json.dumps({"urla": x['copyURL'], "urlatext": "原文链接：" + x['source'],
                               "targeta": 1}, ensure_ascii=False)
        alias = 'p%d' % int(x['id']) if is_page else 'c%d-%s' % (jcat, x['id'])
        pup = "'%s'" % x['addedDate'] if (x['addedDate'] or '').strip() not in ('', '0000-00-00 00:00:00') else 'NULL'
        rows.append({
            'newid': newid, 'oldid': x['id'], 'title': x['title'], 'alias': alias,
            'body': body, 'summary': summary, 'kw': x['keywords'],
            'created': x['addedDate'], 'modified': x['editedDate'],
            'by': x['author'] or x['addedBy'] or '管理员',
            'hits': x['views'] or '0', 'feat': 1 if x['sticky'] not in ('0', '') else 0,
            'cat': jcat, 'urls': urls, 'ext': ext, 'desc': desc120, 'pup': pup,
            'note': 'external' if ext else '',
            'attribs': '{"ext_link":"1"}' if ext else '{}'})
        amap.append((x['id'], newid, x['type'], jcat, bool(ext)))
        # 301: 旧URL → 新文章(站外文直跳外链)
        if is_page:
            old = '/page/%s.html' % (x['alias'] or x['id'])
            redirects.append((old, 'index.php?option=com_content&view=article&id=%d' % newid))
        else:
            oldcat = next((cc for cc in byart.get(x['id'], []) if cc in cats), None)
            if oldcat and cats[oldcat]['alias']:
                old = '/%s/%s.html' % (cats[oldcat]['alias'], x['id'])
                redirects.append((old, ext if ext else
                                  'index.php?option=com_content&view=article&id=%d' % newid))

    tag = 'trial' if trial else 'full'
    # ---------- 写出 SQL ----------
    with open(OUT + '/categories.sql', 'w', encoding='utf-8') as f:
        f.write('-- Joomla分类导入(14个:13频道+协会概况,IDs %d..%d),适配全新Joomla5空站\n' % (CAT_BASE, PAGE_CAT))
        f.write("SELECT @cr := MAX(rgt) FROM `#__categories`;\n")
        f.write("UPDATE `#__categories` SET lft = lft + %d WHERE lft > @cr;\n" % (2 * NCAT))
        f.write("UPDATE `#__categories` SET rgt = rgt + %d WHERE rgt >= @cr;\n" % (2 * NCAT))
        f.write("SELECT @amax := MAX(id), @armax := MAX(rgt) FROM `#__assets`;\n")
        f.write("SELECT @cc := id, @ccl := level FROM `#__assets` WHERE name = 'com_content' LIMIT 1;\n")
        f.write("SELECT @ar := rgt FROM `#__assets` WHERE name = 'com_content' LIMIT 1;\n")
        vals = []
        avals = []
        for i, (jid, name, alias) in enumerate(jcats):
            vals.append("(%d, @amax+%d, 1, @cr+%d, @cr+%d, 1, '%s', 'com_content', '%s', '%s', '', 1, 1, '{}', '', '', '{\"language\":\"*\"}', 62, NOW(), 0, '0000-00-00 00:00:00', 0, '*', 1)"
                         % (jid, i + 1, 2 * i, 2 * i + 1, alias, esc(name), alias))
            avals.append("(@amax+%d, @cc, @armax+%d, @armax+%d, @ccl+1, 'com_content.category.%d', '%s', '{}')"
                         % (i + 1, 2 * i, 2 * i + 1, jid, esc(name)))
        f.write("UPDATE `#__assets` SET lft = lft + %d WHERE lft > @armax;\n" % (2 * NCAT))
        f.write("UPDATE `#__assets` SET rgt = rgt + %d WHERE rgt >= @armax;\n" % (2 * NCAT))
        f.write("INSERT INTO `#__assets` (id, parent_id, lft, rgt, level, name, title, rules) VALUES\n" +
                ",\n".join(avals) + ";\n")
        f.write("INSERT INTO `#__categories` (id, asset_id, parent_id, lft, rgt, level, path, extension, title, alias, note, published, access, params, metadesc, metakey, metadata, created_user_id, created_time, modified_user_id, modified_time, hits, language, version) VALUES\n" +
                ",\n".join(vals) + ";\n")

    with open(OUT + '/content_%s.sql' % tag, 'w', encoding='utf-8') as f:
        f.write('-- 内容导入(%s, %d篇). 先执行 categories.sql\n' % (tag, len(rows)))
        f.write("-- 文章IDs %d..%d, 资产IDs %d..%d(显式指定,导入前请确认无冲突)\n" % (ART0, ART0 + len(rows) - 1, AST0, AST0 + len(rows) - 1))
        f.write("SELECT @cc2 := id FROM `#__assets` WHERE name = 'com_content' LIMIT 1;\n")
        # 文章 asset 行: parent 暂挂 com_content(导入后 Joomla 重建嵌套无妨,article 资产按分类挂需已知分类asset id=@a+k)
        avals, cvals, rvals = [], [], []
        for i, r in enumerate(rows):
            nid = r['newid']
            aid = AST0 + i
            cvals.append("(%s, %s, '%s', '%s', '%s', '%s', 1, %s, '%s', 62, '%s', '%s', 0, NULL, %s, NULL, '{}', '%s', '%s', 1, 0, '%s', '%s', 1, %s, %s, '*', '{}', '%s')"
                         % (nid, aid, esc(r['title']), esc(r['alias']), esc(r['summary']), esc(r['body']),
                            r['cat'] if isinstance(r['cat'], int) else CAT_BASE + 1,
                             esc(r['created']), esc(r['by']), esc(r['modified']), r['pup'], esc(r['urls']),
                             esc(r['attribs']), esc(r['kw']), esc(r['desc']), esc(r['hits']), r['feat'], esc(r['note'])))
            avals.append("(%s, @cc2, @tar+%d, @tar+%d, @ccl2+1, 'com_content.article.%s', '%s', '{}')"
                         % (aid, 2 * i, 2 * i + 1, nid, esc(r['title'])))
        # 文章资产: 显式ID(挂 com_content 下)
        f.write("SELECT @ccl2 := level FROM `#__assets` WHERE name = 'com_content' LIMIT 1;\n")
        f.write("SELECT @tar := MAX(rgt) FROM `#__assets`;\n")
        f.write("UPDATE `#__assets` SET lft = lft + %d WHERE lft > @tar;\n" % (2 * len(rows)))
        f.write("UPDATE `#__assets` SET rgt = rgt + %d WHERE rgt >= @tar;\n" % (2 * len(rows)))
        f.write("INSERT INTO `#__assets` (id, parent_id, lft, rgt, level, name, title, rules) VALUES\n" +
                ",\n".join(avals) + ";\n")
        f.write("INSERT INTO `#__content` (id, asset_id, title, alias, introtext, `fulltext`, state, catid, created, created_by, created_by_alias, modified, modified_by, checked_out_time, publish_up, publish_down, images, urls, attribs, version, ordering, metakey, metadesc, access, hits, featured, language, metadata, note) VALUES\n" +
                ",\n".join(cvals) + ";\n")
        for o, n in redirects:
            rvals.append("('%s', '%s', '', '', 0, 1, NOW(), '0000-00-00 00:00:00', 301)" % (esc(o), esc(n)))
        if rvals:
            f.write("INSERT INTO `#__redirect_links` (old_url, new_url, referer, comment, hits, published, created_date, modified_date, header) VALUES\n" +
                    ",\n".join(rvals) + ";\n")
    # .htaccess 旧URL直跳规则( Joomla 路由会先剥 .html 后缀,插件无法匹配,故走前置规则 )
    with open(OUT + '/htaccess_301_%s.txt' % tag, 'w', encoding='utf-8') as fh:
        fh.write("## 旧站URL 301(%d条) — 插入到 Joomla 规则之前\n" % len(redirects))
        for o, n in redirects:
            old = re.escape(o.lstrip('/'))
            new = '/' + n if not n.startswith('http') else n
            fh.write("RewriteRule ^%s$ %s [R=301,L]\n" % (old, new))
    # 工作流关联(后台文章列表 INNER JOIN 此表,缺失则不可见) — 追加入内容SQL
    with open(OUT + '/content_%s.sql' % tag, 'a', encoding='utf-8') as f:
        # 工作流关联(后台文章列表 INNER JOIN 此表,缺失则不可见)
        wvals = ["(%d, (SELECT s.id FROM `#__workflow_stages` s JOIN `#__workflows` w ON w.id = s.workflow_id WHERE w.`default` = 1 AND w.extension = 'com_content.article' ORDER BY s.id LIMIT 1), 'com_content.article')" % r['newid'] for r in rows]
        f.write("INSERT INTO `#__workflow_associations` (item_id, stage_id, extension) VALUES\n" +
                ",\n".join(wvals) + ";\n")

    with open(OUT + '/map_article_%s.csv' % tag, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['old_id', 'new_id_expr', 'type', 'joomla_cat', 'is_external'])
        w.writerows(amap)

    # ---------- 暂存镜像 ----------
    staged = 0
    for p, src in sorted(img_map.items()):
        dst = OUT + '/images/' + p
        if os.path.exists(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
            staged += 1

    # ---------- 校验报告 ----------
    rep = []
    rep.append('# ETL试迁报告(%s)' % tag if trial else '# ETL全量报告')
    rep.append('')
    rep.append('- 处理文章: %d (单页 %d / 孤儿并入 %d / 站外直跳 %d)' % (
        len(rows), sum(1 for r in amap if r[2] == 'page'), orphan_cnt, link_cnt))
    rep.append('- 生成301: %d 条' % len(redirects))
    rep.append('- 内链图片改写成功/缺失: %d/%d' % (stats['img_ok'], stats['img_missing']))
    rep.append('- 暂存镜像: %d 个' % staged)
    rep.append('- 改写后仍缺失文件: %d' % len(missing))
    for m in sorted(set(missing))[:50]:
        rep.append('  - MISSING ' + m)
    if len(set(missing)) > 50:
        rep.append('  - ... 共 %d 个(见上)' % len(set(missing)))
    fn = OUT + ('/TRIAL_REPORT.md' if trial else '/FULL_REPORT.md')
    open(fn, 'w', encoding='utf-8').write('\n'.join(rep) + '\n')
    print('\n'.join(rep[:8]))
    print('missing sample:', sorted(set(missing))[:5])


if __name__ == '__main__':
    main()
