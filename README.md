# chanzhi-to-joomla — 蝉知 CMS → Joomla 5 迁移工具链

把一座运行中的蝉知（ZSITE 8.x, `eps_*` 表）官网，1:1 搬到 Joomla 5。
含：备份解析 → ETL → 菜单模块 → 多栏目补副本 → 增量追平 → 301/Sitemap → 模板 1:1 校验。
全程默认 dry-run：只产出 SQL 文件，确认无误再 `--apply` 写库。

实战规模参考：1500+ 篇文章，360+ 篇一文多栏目，补 380+ 行副本。

## 0. 前置

- 目标机：PHP + MariaDB/MySQL 客户端 + Joomla 5 空站；截图栈另需 Firefox + geckodriver + `pip install -r requirements.txt`。
- 源站两份备份：**mysqldump SQL** + **整站文件包**（图片/附件靠它还原）。
- 配置（只改这一个文件）：

```bash
cp tools/config.py tools/config.py   # 直接改
export CHANZHI_SQL=/path/to/source.sql
export CHANZHI_WEBDATA=/path/to/unpacked/www/data
export JDB_HOST=localhost JDB_NAME=joomla JDB_USER=joomla JDB_PASS=xxx
export JOOMLA_ROOT=/var/www/html IMAGE_BASE=/images/site
```

`tools/config.py` 里另有栏目映射（`ALIVE_ORDER`/`JCAT_BASE`）、菜单（`MENU_CATS`/`HIDDEN_CATS`/`CHAN_ORDER`）、单页、Sitemap 正文等示例值，按你的站替换。

## 1. 解析备份（只读，永远先跑这个）

```bash
python3 tools/dump_table.py eps_article id,title,type,status,addedDate
python3 tools/dump_table.py eps_relation          # type,id,category,lang
python3 tools/dump_table.py eps_category id,name,alias
```

先回答三个问题再动手：内容–栏目是 1 对 1 还是 1 对多？孤儿栏目（relation 指向不存在分类）怎么并入？哪些是站外文（`link` 非空）？

## 2. ETL 试迁 → 全量

```bash
python3 tools/chanzhi_to_joomla.py --trial   # 抽样100篇: 全部单页+置顶+站外/孤儿/转载+每类约5
python3 tools/chanzhi_to_joomla.py --full    # 全量
```

产出 `out/`：`categories.sql` → `content_{trial,full}.sql`（含 301 与 `#__workflow_associations`，**缺了工作流关联后台文章列表不可见**）→ `htaccess_301_*.txt` → `*_REPORT.md`（缺失文件清单）→ `images/` 暂存镜像 → `map_article_*.csv`（新旧 id 对照，后续幻灯/增量全靠它）。

正文改写口径：`/file.php?f=路径&t=后缀`（**先去掉 `&o=&s=&v=` 尾巴再取文件**）→ `IMAGE_BASE/...`；磁盘文件多无后缀，用 `eps_file.pathname→extension` 还原；文档类附件在文末追加“附件下载”段；绝对域名剥成站内相对。

导入顺序：categories → content → menus_modules → 拷 images → 测 301。

## 3. 菜单、模块、Sitemap

```bash
python3 tools/make_menus_modules.py        # 导航+隐藏路由(hiddenmenu 供无导航位栏目出 SEF 链接)+首页模块
python3 tools/add_sitemap.py               # 站点地图文章+隐藏菜单，幂等
```

## 4. 一文多栏目补副本（最容易漏的一步）

```bash
python3 tools/multichannel_dup.py           # check: 打印每栏待补数
python3 tools/multichannel_dup.py --apply   # 写库
```

以**目标库实际存在行**为准判定缺谁，不预设主栏目。副本 alias 规范 `c<新栏目id>-<源id>`（一眼溯源），`featured=0`，asset 行挂 `parent_id=<com_content asset id>`。

## 5. 增量追平（源站是活的，必须做）

割接前再拿一份新备份，三路 diff：新增走增量入库、删除的只下架（`state=0`，301 改指上级栏目）、变更的重刷：

```bash
python3 tools/refresh_pages.py --apply --retire 2007,2008
python3 tools/rebuild_bodies.py --pair <新id>:<源id>:<源页面html> [--check]
```

教训：栏目落地页只显示最新 N 条，翻页可能是假的——**比对以 SQL 为准，线上只抽查**；源站可能限流，抓取单请求加间隔。

## 6. 模板 1:1 与栏目数核对

```bash
python3 tools/shot.py <url> --txt dom.html     # 截图+元素几何+坏图清单
python3 tools/gaps.py <url>                      # 垂直留白排序，定位异常空白
python3 tools/probe.py <url> <y0> <y1>           # 某 y 区间元素树
python3 tools/cmp.py <源站URL> <新站URL>          # 同区并排对比
```

- 碰到“改了 CSS 前台不变”：模板 `user.css?v=` 查询串、模块缓存、浏览器缓存三层逐一清。
- 覆盖 Joomla 默认样式时注意**选择器权重**（如 `.grid-child` 要 `(0,3,1)` 以上才能打平）。
- 栏目数核对：`SELECT catid,COUNT(*) FROM jos_content WHERE state=1 GROUP BY catid`，与“主行 + 副本”预期逐栏对上；抽查跨栏文章双栏 URL 都是 200。

## 7. 运营设置（SQL 一行流）

```sql
-- 浏览器标题: 菜单 params 写 page_title, 分类菜单锁 show_page_heading=0
-- 栏目置顶: 菜单排序 orderby_sec=front (推荐优先、其余日期)；首页栏目块用置顶优先布局
-- 以后置顶一律点文章星标，不要改排序值
```

## 8. 数据库铁律（血泪）

- CLI 写 `jos_` 前缀（`#__` 会被当注释）；`fulltext` 等保留字加反引号。
- `mariadb --batch`：NULL 显示为字面量 `NULL`（**不是 `\N`**），字符串已转义——解析还原、回写只加引号，**绝不二次转义**，否则 `\'` 变 `\\\'` 直接 1064。
- `subprocess` 的 stdout 是字符串，取行用 `.splitlines()`。
- `jos_assets.rules` 无默认值，INSERT 显式带；写库先校验（id 连续、alias 唯一、assets lft/rgt 成对、asset_id 对应），失败整批回滚并确认计数归零。

## 9. AI 结对建议

这套流程适合人机结对：人讲现象和验收标准（“两栏之间要和源站一样 14px”），AI 写 CSS/模板/脚本；AI 的每条 SQL 和文件改动必须过第 8 节校验或人工抽查。体力活给 AI，把关留给人。

## 目录

```
tools/  dump_table.py 备份解析
        chanzhi_to_joomla.py 主ETL
        make_menus_modules.py 菜导/模块
        add_sitemap.py 站点地图
        multichannel_dup.py 多栏目副本
        refresh_pages.py / rebuild_bodies.py 增量
        shot.py / gaps.py / probe.py / cmp.py 截图校验栈
        config.py 唯一需要改的配置
docs/   playbook.md 完整复盘备忘
        pitfalls-wechat.md 公众号文章
        pitfalls-planet.md 知识星球分享
```

## 不在仓库里的东西

源站备份 SQL/文件包、密码、文章正文、带站名的模板与截图——体积大且涉密，永远不要提交（见 `.gitignore`）。
