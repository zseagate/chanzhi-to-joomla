# 迁移复盘备忘（ playbook ）

> 一次蝉知 ZSITE 8.x → Joomla 5 的全流程记录：1500+ 篇，360+ 篇一文多栏目，源站迁移期持续更新。
> 匿名版，命令与脚本见本仓库 `tools/`，心态与故事见 `pitfalls-wechat.md / pitfalls-planet.md`。

## 阶段 0：摸底（不动库，只读）

1. `dump_table.py` 看四张表：`eps_article`（id/title/type/status/addedDate）、`eps_relation`（列序 `type,id,category,lang`）、`eps_category`、`eps_file`。
2. 回答：1 对 1 还是 1 对多？孤儿栏目映射？站外文（`link` 非空）几篇？附件存哪（多无后缀）？
3. 产出栏目映射表：存活栏目按优先级排，孤儿逐个指定并入目标。

## 阶段 1：ETL 试迁

1. 填 `config.py`，跑 `--trial`（分层抽样 100：全部单页 + 置顶 + 站外 + 孤儿 + 转载 + 每类约 5）。
2. 看 `TRIAL_REPORT.md` 的 MISSING 清单：缺文件先从文件包找，找不到记账不阻塞。
3. 抽查三类页面：单页、站外文（标题是否直跳外链）、带附件文章（下载段+后缀对不对）。
4. 全量 `--full`，按 categories → content（含工作流关联！）→ menus → images → 301 顺序导入空站。

## 阶段 2：模板 1:1

1. `shot.py` 整页截图 + `--txt` 落 DOM；`gaps.py` 找异常留白；`cmp.py` 源站/新站并排。
2. 空白类问题：从 padding/margin/图片自适应高度三处叠加查起，用 `probe.py` 看 y 区间元素树。
3. CSS 改完 bump 查询串；布局切换期关模块缓存；清站缓存+浏览器硬刷。

## 阶段 3：多栏目补副本

1. `multichannel_dup.py` 先 check：总数与“多栏目篇数”对上，各栏增量与预期对上。
2. 校验 SQL：content id 连续、alias 唯一、assets lft/rgt 成对、`asset_id` 对应、新增区间库内为 0。
3. `--apply` 看返回码，非 0 查计数回滚。曾连跪两次：二次转义 1064、`publish_down='NULL'`、缺 `rules` 列。

## 阶段 4：增量追平

1. 新备份三路 diff：新增 / 删除 / 变更。
2. `refresh_pages.py --apply --retire <已删本地id>`；新增走 `rebuild_bodies.py`。
3. 删文 301 改指上级栏目；复查栏目计数。

## 阶段 5：运营与收尾

1. 菜单 `page_title` 写浏览器标题并锁 `show_page_heading=0`；栏目排序 `orderby_sec=front`；首页块置顶优先布局。
2. 栏目计数 `GROUP BY catid` 逐栏核对；跨栏文章双 URL 抽查 200；Sitemap/301 全量过一遍。
3. 建编辑账号（随机密码 bcrypt 入库，Manager+Publisher 组），开 TOTP，写使用手册。

## 阶段 6：割接后持续同步（老站还在更新）

1. 常态通道 `sync_source.php`（部署在站点 `cli/`，单文件 PHP，dry-run 默认、`--apply` 实写、
   每篇一个事务、写后自动校验）。
2. **发现用老站 sitemap.xml**：单请求拿全量清单 − 本地 alias 反解的已有集，天然幂等；
   落地页只作交叉发布归属的补充（它只显示最新几条、翻页是假的）。
3. **主栏目 = 落地页链接的 slug**（CMS 规范链接；sitemap 列的栏目可能是另一个交叉栏目，
   两个口径不一致时两条老 URL 都写 301）。纯备份推不出规范链接——`eps_relation`
   无排序字段，首行规则实测约 7/10 吻合；已在目标库的文章一律沿用目标库主/副本
   （多站用同一份 bundle 各导一次即收敛一致）。
4. **频控血泪**：一天约十轮全量抓取 → IP 被 TCP 级封禁（本机+中转隧道全废）。
   探查用 `--no-landing`（一个请求），全量每天最多一轮；被封走备份通道别硬抓。
5. **境外服务器直连境内老站可能被断**：`curl -4v` 卡在 `Trying <ip>` 即是。解法：
   国内机器 `ssh -R 1080 user@server` 反向 SOCKS + `--proxy=socks5h://127.0.0.1:1080`。
6. **备份通道更优**（`sync_bundle_from_backup.py` → `--bundle` 导入）：零访问老站，
   还能带上抓取拿不到的置顶/关键词/摘要/附件表；缺文件自动回退老站绝对 URL
   （访客浏览器可加载，老站只封服务器 IP）。
7. 每次同步后的验收：日志尾「校验异常 0」；`curl -sI <老URL>` 应 301；新文章页 200 且
   正文无 `file.php` 残留；跨栏文章各栏目列表都能看到。

## 每次写库前默念

check → 校验（行数/区间/抽样）→ apply → 重查计数 → curl 抽 URL → 截图留证。
