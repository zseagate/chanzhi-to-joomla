#!/bin/bash
# publish_github.sh — 把本地 build/tools 的改动发布到 GitHub 仓库（默认 check，不写不改）
#
# 背景: 本地工作区是「站点专用版」(硬编码 sxpg 站点/库名/路径)，GitHub 上的
#       zseagate/chanzhi-to-joomla 是「脱敏泛化版」(tools/config.py + example.com)。
#       两边不能整目录覆盖，本脚本负责: 拉取远程 → 比对 → 敏感串扫描 → 选择性拷贝 →
#       人读 diff → 可选 commit/push。
#
# 用法:
#   ./build/tools/publish_github.sh                      # 只报告差异(默认, 不碰远程)
#   ./build/tools/publish_github.sh --commit             # 把「新增文件」拷进克隆并提交
#   ./build/tools/publish_github.sh --commit --update    # 连「已存在但有差异」的文件也覆盖
#   ./build/tools/publish_github.sh --commit --push      # 提交并推送
#   ./build/tools/publish_github.sh --include=sync_source.php,sync_slides.php --commit
#   ./build/tools/publish_github.sh --msg="feat: xxx" --commit --push
#
# 选项:
#   --check            只报告(默认)
#   --commit           在克隆里 git add + commit
#   --push             commit 后 push 到 origin/<branch>
#   --update           已存在文件有差异时也覆盖(默认跳过, 提示人工移植)
#   --include=a,b,c    只处理这些文件(文件名, 相对 build/tools 或 tools/)
#   --msg=...          commit message
#   --branch=main      目标分支(默认 main)
#   --remote=URL       仓库地址(默认 git@github.com:zseagate/chanzhi-to-joomla.git)
#   --work=DIR         克隆缓存目录(默认 ~/.cache/chanzhi-to-jojoola-publish)
#   --allow-sensitive  敏感串扫描命中仍继续(慎用; 默认拒绝该文件)
#
# 约定:
#   - 仓库里由 tools/config.py 驱动的文件(chanzhi_to_joomla.py / dump_table.py / shot.py 等)
#     默认视为 MANUAL: 本地版硬编码了站点路径, 直接覆盖会破坏脱敏结构 → 手工移植。
#   - 默认绝不拷贝 .dbpass*、__pycache__、sql_src/、out/、截图、备份 SQL。
set -uo pipefail
cd "$(dirname "$0")/../.."

REMOTE="${REMOTE:-git@github.com:zseagate/chanzhi-to-joomla.git}"
BRANCH="main"
WORK="${XDG_CACHE_HOME:-$HOME/.cache}/chanzhi-to-joomla-publish"
DO_COMMIT=0; DO_PUSH=0; DO_UPDATE=0; ALLOW_SENS=0
INCLUDE=""; MSG=""

for a in "$@"; do
    case "$a" in
        --check) : ;;
        --commit) DO_COMMIT=1 ;;
        --push) DO_COMMIT=1; DO_PUSH=1 ;;
        --update) DO_UPDATE=1 ;;
        --allow-sensitive) ALLOW_SENS=1 ;;
        --include=*) INCLUDE="${a#*=}" ;;
        --msg=*) MSG="${a#*=}" ;;
        --branch=*) BRANCH="${a#*=}" ;;
        --remote=*) REMOTE="${a#*=}" ;;
        --work=*) WORK="${a#*=}" ;;
        -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
        *) echo "未知参数: $a" >&2; exit 1 ;;
    esac
done

# 仓库侧已是泛化实现、直接覆盖会破坏 config.py 结构 → 必须人工移植
MANUAL="add_sitemap.py chanzhi_to_joomla.py dump_table.py shot.py gaps.py cmp.py probe.py
refresh_pages.py make_menus_modules.py multichannel_dup.py sync_bundle_from_backup.py rebuild_bodies.py"

# 站点相关/敏感串(命中即拒绝, 除非 --allow-sensitive)
SENS_RE='(sxpg\.org\.cn|sxpg\.tycx\.com|sxpg4?\.local|sxpgwp\.local|/var/www/sxpg|/www/wwwroot/sxpg|joomla_sxpg|joomla_j4|wp_sxpg|\.dbpass|/home/gw|/home/p50|sxpgadmin|wpsxpgadmin|sxpg_relay)'
# 本脚本自身要发布, 且只包含上述规则字面量 → 免扫
EXEMPT="publish_github.sh"

echo "== 1. 准备克隆 $WORK"
if [ ! -d "$WORK/.git" ]; then
    mkdir -p "$(dirname "$WORK")"
    git clone -q "$REMOTE" "$WORK" || { echo "clone 失败: $REMOTE" >&2; exit 1; }
else
    git -C "$WORK" remote set-url origin "$REMOTE"
    ok=0
    for i in 1 2 3 4; do
        git -C "$WORK" fetch -q origin && { ok=1; break; }
        echo "   fetch 失败($i), 5s 后重试"; sleep 5
    done
    [ "$ok" = 1 ] || { echo "fetch 失败: $REMOTE" >&2; exit 1; }
fi
git -C "$WORK" checkout -q "$BRANCH" 2>/dev/null || git -C "$WORK" checkout -q -b "$BRANCH" "origin/$BRANCH"
git -C "$WORK" reset --hard -q "origin/$BRANCH" || { echo "无法对齐 origin/$BRANCH" >&2; exit 1; }
echo "   基线: $(git -C "$WORK" log --oneline -1)"

is_manual() {
    local f="$1" m
    for m in $MANUAL; do [ "$f" = "$m" ] && return 0; done
    return 1
}
in_include() {
    [ -z "$INCLUDE" ] && return 0
    case ",$INCLUDE," in *",$1,"*) return 0 ;; esac
    return 1
}

NEW=(); CHANGED=(); SAME=(); MANUAL_HIT=(); BLOCKED=()

echo "== 2. 比对 build/tools → tools/"
for src in build/tools/*; do
    [ -f "$src" ] || continue
    name="$(basename "$src")"
    case "$name" in
        *.py|*.php|*.sh) ;;
        *) continue ;;
    esac
    in_include "$name" || continue
    if is_manual "$name"; then MANUAL_HIT+=("$name"); continue; fi
    if [ ! -f "$WORK/tools/$name" ]; then
        NEW+=("$name"); continue
    fi
    if cmp -s "$src" "$WORK/tools/$name"; then
        SAME+=("$name")
    else
        CHANGED+=("$name")
    fi
done

printf "   新增: %s\n" "${NEW[*]:-无}"
printf "   有差异: %s\n" "${CHANGED[*]:-无}"
printf "   跳过(仓库侧已泛化, 需人工移植): %s\n" "${MANUAL_HIT[*]:-无}"
printf "   已一致: %d 个\n" "${#SAME[@]}"

# 敏感串扫描
scan() {
    local f="$1" hits
    hits="$(grep -nE "$SENS_RE" "$f" | head -5)"
    if [ -n "$hits" ]; then
        echo "   [敏感] $(basename "$f"):"
        echo "$hits" | sed 's/^/     /'
        return 1
    fi
    return 0
}

echo "== 3. 敏感串扫描"
check_sens() {
    local name="$1"
    case " $EXEMPT " in *" $name "*) echo "   [免扫] $name (规则文件)"; return 0 ;; esac
    if ! scan "build/tools/$name"; then
        if [ "$ALLOW_SENS" = 1 ]; then
            echo "     --allow-sensitive: 仍继续 $name"
        else
            BLOCKED+=("$name")
        fi
    fi
}
for name in "${NEW[@]}"; do check_sens "$name"; done
if [ "$DO_UPDATE" = 1 ]; then
    for name in "${CHANGED[@]}"; do check_sens "$name"; done
fi
if [ "${#BLOCKED[@]}" -gt 0 ]; then
    echo "   以下文件含站点/口令特征, 未纳入: ${BLOCKED[*]}"
    echo "   → 先脱敏(改 example.com / env 变量 / 去掉路径)再发, 或确认后加 --allow-sensitive"
fi

TO_COPY=()
for name in "${NEW[@]}"; do
    [[ " ${BLOCKED[*]} " == *" $name "* ]] || TO_COPY+=("$name")
done
if [ "$DO_UPDATE" = 1 ]; then
    for name in "${CHANGED[@]}"; do
        [[ " ${BLOCKED[*]} " == *" $name "* ]] || TO_COPY+=("$name")
    done
fi

if [ "${#TO_COPY[@]}" -eq 0 ]; then
    echo "== 没有可自动发布的文件"
    [ "${#CHANGED[@]}" -gt 0 ] && echo "   有 ${#CHANGED[@]} 个已存在文件有差异, 需 --update 或人工移植"
    exit 0
fi

echo "== 4. 待发布: ${TO_COPY[*]}"
if [ "$DO_COMMIT" = 0 ]; then
    for name in "${TO_COPY[@]}"; do
        echo "--- $name (本地 vs 仓库)"
        diff -u "$WORK/tools/$name" "build/tools/$name" 2>/dev/null | head -40 ||
            diff -u /dev/null "build/tools/$name" | head -40
    done
    echo "== check 完毕, 未修改克隆。加 --commit 落地, --commit --push 推送"
    exit 0
fi

for name in "${TO_COPY[@]}"; do
    cp "build/tools/$name" "$WORK/tools/$name"
    echo "   已拷贝 $name"
done
git -C "$WORK" add -A

echo "== 5. diff --stat"
git -C "$WORK" diff --cached --stat

if git -C "$WORK" diff --cached --quiet; then
    echo "== 无改动可提交"; exit 0
fi

if [ -z "$MSG" ]; then
    MSG="chore: 同步本地工具改动 ($(date +%F))"
fi
git -C "$WORK" config user.name >/dev/null || git -C "$WORK" config user.name "zseagate"
git -C "$WORK" config user.email >/dev/null || git -C "$WORK" config user.email "zseagate@users.noreply.github.com"
git -C "$WORK" commit -q -m "$MSG" || { echo "commit 失败" >&2; exit 1; }
echo "   已提交: $(git -C "$WORK" log --oneline -1)"

if [ "$DO_PUSH" = 1 ]; then
    echo "== 6. push origin/$BRANCH"
    for i in 1 2 3; do
        if GIT_SSH_COMMAND="ssh -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=4" \
            git -C "$WORK" push origin "$BRANCH"; then
            echo "   推送 OK"
            break
        fi
        echo "   第 $i 次推送失败, 5s 后重试"
        sleep 5
        [ "$i" = 3 ] && { echo "推送失败, 克隆里已提交, 可稍后手动 push" >&2; exit 1; }
    done
else
    echo "== 已提交到 $WORK (未推送)。推送: git -C $WORK push origin $BRANCH"
fi
