#!/usr/bin/env python3
"""创建站点地图文章 + 隐藏菜单(/sitemap)。幂等: 已存在则跳过。
用法: python3 tools/add_sitemap.py [--id 3601] [--cat 1014] [--menu 1101]
正文 BODY 在 tools/config.py 里改。
"""
import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import JDB_HOST, JDB_NAME, JDB_USER, JDB_PASS, SITEMAP_BODY  # noqa: E402

HOST, DB, USER, PW = JDB_HOST, JDB_NAME, JDB_USER, JDB_PASS
BODY = SITEMAP_BODY.replace("'", "\\'")


def q(sql):
    r = subprocess.run(['mariadb', '-h' + HOST, '-u' + USER, '-p' + PW, DB, '-N', '-B', '-e', sql],
                       capture_output=True, text=True)
    if r.stderr.strip():
        print('ERR:', r.stderr.strip()[:300])
    return r.stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--id', type=int, default=3601)
    ap.add_argument('--cat', type=int, default=1014)
    ap.add_argument('--menu', type=int, default=1101)
    ap.add_argument('--uid', type=int, default=62, help='创建人用户id(通常是初始超管)')
    a = ap.parse_args()
    nid = a.id
    if q("SELECT id FROM jos_content WHERE id=%d" % nid):
        print('sitemap article exists, skip')
        return
    amax = int(q("SELECT MAX(id) FROM jos_assets").split()[0])
    armax = int(q("SELECT MAX(rgt) FROM jos_assets").split()[0])
    mmax = int(q("SELECT MAX(rgt) FROM jos_menu").split()[0])
    q(f"UPDATE jos_assets SET lft = lft + 2 WHERE lft > {armax}")
    q(f"UPDATE jos_assets SET rgt = rgt + 2 WHERE rgt >= {armax}")
    q(f"INSERT INTO jos_assets (id,parent_id,lft,rgt,level,name,title,rules) VALUES "
      f"({amax+1},8,{armax},{armax+1},2,'com_content.article.{nid}','站点地图','{{}}')")
    q(f"INSERT INTO jos_content (id,asset_id,title,alias,introtext,`fulltext`,state,catid,"
      f"created,created_by,created_by_alias,modified,modified_by,checked_out_time,publish_up,"
      f"publish_down,images,urls,attribs,version,ordering,metakey,metadesc,access,hits,featured,"
      f"language,metadata,note) VALUES ({nid},{amax+1},'站点地图','sitemap','','{BODY}',1,{a.cat},"
      f"NOW(),{a.uid},'管理员',NOW(),0,NULL,NULL,NULL,'{{}}','','{{}}',1,0,'','',1,0,0,'*','{{}}','')")
    q(f"INSERT INTO jos_workflow_associations (item_id,stage_id,extension) VALUES "
      f"({nid},1,'com_content.article')")
    if not q("SELECT menutype FROM jos_menu_types WHERE menutype='hiddenmenu'"):
        q("INSERT INTO jos_menu_types (menutype,title,description,client_id) "
          "VALUES ('hiddenmenu','Hidden Menu','',0)")
    q(f"UPDATE jos_menu SET lft = lft + 2 WHERE lft > {mmax}")
    q(f"UPDATE jos_menu SET rgt = rgt + 2 WHERE rgt >= {mmax}")
    q(f"INSERT INTO jos_menu (id,menutype,title,alias,note,path,link,type,published,parent_id,"
      f"level,component_id,checked_out,checked_out_time,browserNav,access,img,template_style_id,"
      f"params,lft,rgt,home,language,client_id,publish_up,publish_down) VALUES "
      f"({a.menu},'hiddenmenu','站点地图','sitemap','','sitemap',"
      f"'index.php?option=com_content&view=article&id={nid}','component',1,1,1,19,NULL,NULL,0,1,'',0,"
      f"'{{\"menu_text\":1,\"menu_show\":1}}',{mmax},{mmax+1},0,'*',0,NULL,NULL)")
    print('sitemap created:', q("SELECT id,title FROM jos_content WHERE id=%d" % nid))


if __name__ == '__main__':
    main()
