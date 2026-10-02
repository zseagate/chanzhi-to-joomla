#!/usr/bin/env python3
"""生成导航菜单+首页模块的 Joomla SQL。菜单/模块ID显式指定。
前置: 先填好 tools/config.py 的 MENU_CATS / HIDDEN_CATS / CHAN_ORDER。
产出: out/menus_modules.sql (表前缀为 #__, 导入时按目标库替换)。
约定示例: 分类IDs 1001..1014; Home菜单id=101。
"""
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dump_table import split_tuples, sql_unescape  # noqa: E402
from chanzhi_to_joomla import resolve_file, DATADIR, esc, load, OUT  # noqa: E402

from config import MENU_CATS, HIDDEN_CATS, CHAN_ORDER  # noqa: E402
from config import ASSOC_MENU, BANNER_BLOCKS, HERO_IMAGE  # noqa: E402
from config import ASSOC_MENU_ID, CAT_MENU_BASE, HIDDEN_MENU_BASE, IMAGE_BASE  # noqa: E402
# config 示例:
# ASSOC_MENU = {'title': '协会概况', 'alias': 'about', 'article_id': 2003,
#               'children': [('简介', 'intro', 2003), ('章程', 'charter', 2002)]}
# BANNER_BLOCKS = [(1015, '顶部banner', 'banner', 238), ...]  # (模块id, 标题, 位置, eps_block.id)
# HERO_IMAGE = 'source/default/default/xxx.pic.jpg'  # 头部通栏大图在旧包中的路径, '' 则跳过
# (子菜单结构请直接写进 config.ASSOC_MENU['children'], 见上例)


def block_html(bid):
    for b in load('eps_block'):
        if b['id'] == str(bid):
            # load()已做SQL反转义，直接解析；兼容旧行为再试一次
            for c in (b['content'],):
                try:
                    return json.loads(c).get('content', '')
                except Exception:
                    pass
            try:
                return json.loads(sql_unescape(b['content'])).get('content', '')
            except Exception:
                return ''
    return ''


def rewrite_imgs(html, img_map, missing):
    def rep(m):
        fpath = m.group(1).replace('&amp;', '&')
        tm = re.search(r'[?&]t=([a-zA-Z0-9]+)', m.group(2).replace('&amp;', '&'))
        staged, disk = resolve_file(fpath, tm.group(1) if tm else '', {})
        if staged:
            img_map[staged] = disk
            return IMAGE_BASE + '/' + staged
        missing.append(fpath)
        return m.group(0)
    return re.sub(r'/file\.php\?f=([^&"\'\s<>]+)([^"\'\s<>]*)', rep, html)


def main():
    img_map, missing = {}, []
    # --- 新旧文章ID对照(幻灯外链映射) ---
    idmap = {}
    for fn in ('map_article_full.csv', 'map_article_trial.csv'):
        p = OUT + '/' + fn
        if os.path.exists(p):
            import csv as _csv
            for r in _csv.DictReader(open(p, encoding='utf-8')):
                idmap[r['old_id']] = r['new_id_expr']
            break
    # --- 幻灯(7张,group=73): 图片 + 链到新文章 ---
    slides = [s for s in load('eps_slide')]
    simgs = []
    for s in slides:
        m = re.search(r'/file\.php\?f=([^&"\'\s<>]+)([^"\'\s<>]*)', s.get('image', ''))
        if not m:
            continue
        fpath = m.group(1)
        tm = re.search(r'[?&]t=([a-zA-Z0-9]+)', m.group(2).replace('&amp;', '&'))
        staged, disk = resolve_file(fpath, tm.group(1) if tm else '', {})
        if not staged:
            missing.append(fpath)
            continue
        img_map[staged] = disk
        link, tgt = '', '_self'
        lm = re.search(r'[?&]id=(\d+)', (s.get('mainLink', '') or '').replace('&amp;', '&'))
        if lm and lm.group(1) in idmap:
            link = 'index.php?option=com_content&view=article&id=%s' % idmap[lm.group(1)]
            tgt = '_blank' if (s.get('target', '') or '') == '1' else '_self'
        simgs.append((staged, s.get('title', ''), link, tgt))
    slide_html = '<div class="mig-slides">' + ''.join(
        '<div>' + (('<a href="/%s" target="%s">' % (l, t)) if l else '') +
        '<img src="%s/%s" alt="%s" loading="lazy">' % (IMAGE_BASE, p, esc(x or '幻灯')) +
        ('</a>' if l else '') + '</div>'
        for p, x, l, t in simgs) + '</div>'
    # --- 头部通栏大图(舞台背景) ---
    hb, _disk = resolve_file(HERO_IMAGE, 'jpg', {}) if HERO_IMAGE else (None, None)
    if hb:
        import shutil as _sh
        src = _disk
        dst = OUT + '/images/' + hb
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if not os.path.exists(dst):
            _sh.copy2(src, dst)

    def custom(html):
        return rewrite_imgs(html, img_map, missing)

    mods = []  # (id, title, module, position, ordering, content/params, showtitle, published)
    mid = 1001
    for title, cat in CHAN_ORDER:
        is_top = (cat == '1001')
        mods.append((mid, title, 'mod_articles_category',
                     'top-a' if is_top else 'main-bottom',
                     2 if is_top else mid - 1000,
                     '{"catid":["%s"],"count":"8","show_date":"1","show_date_format":"Y-m-d","link_titles":"1","article_ordering":"publish_up","article_ordering_direction":"DESC"}' % cat, 1, 1))
        mid += 1
    mods.append((1014, '首页轮播', 'mod_custom', 'top-a', 1, slide_html, 0, 1))
    for mid_, mtitle, pos, bid in BANNER_BLOCKS:
        mods.append((mid_, mtitle, 'mod_custom', pos, 1, custom(block_html(bid)), 0, 1))
    # 友链: config links.index(已在P0确认为10个链接HTML)
    links = ''
    for c in load('eps_config'):
        if c['section'] == 'links' and c['key'] == 'index':
            links = sql_unescape(c['value'])
    mods.append((1019, '友情链接', 'mod_custom', 'footer', 1, links, 1, 1))
    beian = ('<div class="site-beian"><a href="/sitemap">站点地图</a>&ensp;|&ensp;© <span class="site-org">贵单位名称</span></div>')
    mods.append((1020, '版权备案', 'mod_custom', 'footer', 2, beian, 0, 1))
    welcome = ('<div class="site-welcome">欢迎光临！今天是 <span id="site-date"></span></div>'
               '<script>var _t=new Date(),_y=_t.getFullYear(),_m=_t.getMonth()+1,_d=_t.getDate(),_w=["星期日","星期一","星期二","星期三","星期四","星期五","星期六"][_t.getDay()],_h=_t.getHours(),_g=(_h>=8&&_h<=12)?" 上午好":"";document.getElementById("site-date").textContent=_y+"年"+_m+"月"+_d+"日 "+_w+_g;</script>')
    mods.append((1021, '欢迎条', 'mod_custom', 'topbar', 1, welcome, 0, 1))
    mods.append((1022, '站内搜索', 'mod_finder', 'search', 1,
                 '{"show_button":"0","show_autosuggest":"1","field_size":"20","show_advanced":"0","show_label":"0","moduleclass_sfx":"","cache":"0"}', 0, 1))

    with open(OUT + '/menus_modules.sql', 'w', encoding='utf-8') as f:
        f.write('-- 导航+首页模块. 前提: categories.sql已导入(分类1001..1014),内容已导入(单页2001+)\n')
        f.write("UPDATE `#__menu` SET title = '首页' WHERE id = 101;\n")
        f.write("SELECT @mm := MAX(rgt) FROM `#__menu`;\n")
        # 槽位按实际条目数动态计算: 父项(2槽)+子项*2 + 频道*2 + 隐藏路由*2
        children = (ASSOC_MENU or {}).get('children', [])
        n_assoc = (1 + len(children)) if ASSOC_MENU else 0
        total_slots = 2 * (n_assoc + len(MENU_CATS) + len(HIDDEN_CATS))
        f.write("UPDATE `#__menu` SET lft = lft + %d WHERE lft > @mm;\n" % total_slots)
        f.write("UPDATE `#__menu` SET rgt = rgt + %d WHERE rgt >= @mm;\n" % total_slots)
        mvals = []
        cur = 0
        if ASSOC_MENU:
            am = ASSOC_MENU
            mvals.append(
                "(%d, 'mainmenu', '%s', '%s', '', '%s', "
                "'index.php?option=com_content&view=article&id=%d', 'component', 1, 1, 1, 19, "
                "NULL, NULL, 0, 1, '', 0, '{\"menu_text\":1,\"menu_show\":1}', @mm+%d, @mm+%d, 0, '*', 0, NULL, NULL)"
                % (ASSOC_MENU_ID, esc(am['title']), am['alias'], am['alias'],
                   am['article_id'], cur, cur + 2 * n_assoc - 1))
            for j, (title, alias, artid) in enumerate(children):
                link = 'index.php?option=com_content&view=article&id=%d' % artid
                mvals.append(
                    "(%d, 'mainmenu', '%s', '%s', '', '%s/%s', '%s', 'component', 1, %d, 2, 19, "
                    "NULL, NULL, 1, 1, '', 0, '{\"menu_text\":1,\"menu_show\":1}', @mm+%d, @mm+%d, 0, '*', 0, NULL, NULL)"
                    % (ASSOC_MENU_ID + 1 + j, esc(title), alias, am['alias'], alias,
                       link, ASSOC_MENU_ID, cur + 1 + 2 * j, cur + 2 + 2 * j))
            cur += 2 * n_assoc
        f.write("INSERT INTO `#__menu` (id, menutype, title, alias, note, path, link, type, published, parent_id, level, component_id, checked_out, checked_out_time, browserNav, access, img, template_style_id, params, lft, rgt, home, language, client_id, publish_up, publish_down) VALUES\n" +
                ",\n".join(mvals) + ";\n")
        # 模块资产
        f.write("SELECT @mx := MAX(id), @mrx := MAX(rgt) FROM `#__assets`;\n")
        f.write("SELECT @moda := id FROM `#__assets` WHERE name = 'com_modules' LIMIT 1;\n")
        f.write("SELECT @modl := level FROM `#__assets` WHERE name = 'com_modules' LIMIT 1;\n")
        f.write("UPDATE `#__assets` SET lft = lft + %d WHERE lft > @mrx;\n" % (2 * len(mods)))
        f.write("UPDATE `#__assets` SET rgt = rgt + %d WHERE rgt >= @mrx;\n" % (2 * len(mods)))
        avals = ["(@mx+%d, @moda, @mrx+%d, @mrx+%d, @modl+1, 'com_modules.module.%d', '%s', '{}')"
                 % (i + 1, 2 * i, 2 * i + 1, mid_, title.replace("'", ""))
                 for i, (mid_, title, mod, pos, order, content, st, pub) in enumerate(mods)]
        f.write("INSERT INTO `#__assets` (id, parent_id, lft, rgt, level, name, title, rules) VALUES\n" +
                ",\n".join(avals) + ";\n")
        modvals = []
        for i, (mid_, title, mod, pos, order, content, st, pub) in enumerate(mods):
            if mod == 'mod_custom':
                params = '{"prepare_content":"1","backgroundimage":"","layout":"_:default","moduleclass_sfx":"","cache":"1","cache_time":"900","module_tag":"div","bootstrap_size":"0","header_tag":"h3","header_class":"","style":"0"}'
                content_q = "'%s'" % esc(content)
            else:
                params = content
                content_q = "''"
            modvals.append("(%d, @mx+%d, '%s', '', %s, %d, '%s', NULL, NULL, NULL, NULL, %d, '%s', %d, '%s', 1, 0, '*')"
                           % (mid_, i + 1, esc(title), content_q, order, pos, pub, mod, st, params))
        f.write("INSERT INTO `#__modules` (id, asset_id, title, note, content, ordering, position, checked_out, checked_out_time, publish_up, publish_down, published, module, showtitle, params, access, client_id, language) VALUES\n" +
                ",\n".join(modvals) + ";\n")
        mmvals = [("(%d, 0)" if mid_ == 1022 else "(%d, 101)") % mid_ for (mid_, *_r) in mods]
        f.write("INSERT INTO `#__modules_menu` (moduleid, menuid) VALUES\n" + ",\n".join(mmvals) + ";\n")

    staged = 0
    for p, src in sorted(img_map.items()):
        dst = OUT + '/images/' + p
        if os.path.exists(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
            staged += 1
    print('menus=9 modules=%d slides=%d staged=%d missing=%s' % (len(mods), len(simgs), staged, sorted(set(missing))))


if __name__ == '__main__':
    main()
