#!/usr/bin/env bash
# 伺服器端部署執行器：post-receive 觸發，依改動分類決定重建／重啟／提醒，build 前快照 image，健康檢查失敗自動回滾。

set -euo pipefail

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$REPO"

STATE_DIR="$REPO/.deploy"
HISTORY="$STATE_DIR/history.tsv"
LOCK="$STATE_DIR/lock"
LOG="$STATE_DIR/apply.log"
mkdir -p "$STATE_DIR"

# shellcheck source=target.conf
. "$REPO/deploy/target.conf"
[ -f "$STATE_DIR/deploy.env" ] && . "$STATE_DIR/deploy.env"

say()  { printf '%s\n' "$*"; printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$LOG"; }
step() { printf '\n── %s\n' "$*"; printf '%s ── %s\n' "$(date '+%F %T')" "$*" >>"$LOG"; }
die()  { printf '\n✗ %s\n' "$*" >&2; printf '%s ✗ %s\n' "$(date '+%F %T')" "$*" >>"$LOG"; exit 1; }

if docker compose version >/dev/null 2>&1; then dc() { docker compose "$@"; }
elif command -v docker-compose >/dev/null 2>&1; then dc() { docker-compose "$@"; }
else die "伺服器上找不到 docker compose"; fi

take_lock() {
    if ! mkdir "$LOCK" 2>/dev/null; then
        local owner; owner=$(cat "$LOCK/owner" 2>/dev/null || echo '?')
        die "另一個部署正在進行（$owner）。確定它已經死了就 rm -rf $LOCK"
    fi
    printf 'pid=%s %s\n' "$$" "$(date '+%F %T')" >"$LOCK/owner"
    trap 'rm -rf "$LOCK"' EXIT
}

NEED_BUILD=0 NEED_UP=0
FILES_IMAGE='' FILES_MOUNT='' FILES_COMPOSE='' FILES_MIGRATION='' FILES_NOTE='' FILES_IDLE=''

classify() {
    local f
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        local matched=0
        if [ -n "$T_IMAGE_RE" ] && printf '%s' "$f" | grep -Eq "$T_IMAGE_RE"; then
            FILES_IMAGE+="  $f"$'\n'; NEED_BUILD=1; matched=1
        fi
        if [ -n "$T_COMPOSE_RE" ] && printf '%s' "$f" | grep -Eq "$T_COMPOSE_RE"; then
            FILES_COMPOSE+="  $f"$'\n'; NEED_UP=1; matched=1
        fi
        if [ -n "$T_MOUNT_RE" ] && printf '%s' "$f" | grep -Eq "$T_MOUNT_RE"; then
            FILES_MOUNT+="  $f"$'\n'; matched=1
        fi
        if [ -n "$T_MIGRATION_RE" ] && printf '%s' "$f" | grep -Eq "$T_MIGRATION_RE"; then
            FILES_MIGRATION+="  $f"$'\n'
        fi
        if [ -n "$T_NOTE_RE" ] && printf '%s' "$f" | grep -Eq "$T_NOTE_RE"; then
            FILES_NOTE+="  $f"$'\n'; matched=1
        fi
        if [ "$matched" = 0 ]; then FILES_IDLE+="  $f"$'\n'; fi
    done
    return 0
}

report_plan() {
    [ -n "$FILES_IMAGE" ]     && { step "要重建 image 的變更"; printf '%s' "$FILES_IMAGE"; }
    [ -n "$FILES_COMPOSE" ]   && { step "compose 設定變更（重新套用）"; printf '%s' "$FILES_COMPOSE"; }
    [ -n "$FILES_MOUNT" ]     && { step "掛載進容器的檔案（換掉就生效，不必重建）"; printf '%s' "$FILES_MOUNT"; }
    [ -n "$FILES_IDLE" ]      && { step "不影響服務的變更（文件、測試、排程腳本…）"; printf '%s' "$FILES_IDLE"; }
    if [ -n "$FILES_NOTE" ]; then
        step "⚠ .env 範本動過"
        printf '%s' "$FILES_NOTE"
        say  "  伺服器的 .env 不在版控裡、這次沒被碰到。比對一下有沒有新的必填欄位："
        say  "  diff <(grep -oE '^[A-Z_]+' .env.example|sort -u) <(grep -oE '^[A-Z_]+' .env|sort -u)"
    fi
    if [ -n "$FILES_MIGRATION" ]; then
        step "⚠ 資料庫 schema 變更 —— 這支腳本不會自動執行"
        printf '%s' "$FILES_MIGRATION"
        say  "  要套用的話自己看過再跑，例如："
        say  "  ${T_MIGRATION_HINT:-docker compose exec -T db psql -U postgres -d <db> < <檔案>}"
    fi
}

health_ok() {
    local out i=0
    while [ "$i" -lt "${T_HEALTH_RETRIES:-24}" ]; do
        if out=$(eval "$T_HEALTH_CMD" 2>/dev/null); then
            if [ -z "${T_HEALTH_OK_RE:-}" ] || printf '%s' "$out" | grep -Eq "$T_HEALTH_OK_RE"; then
                say "健康檢查通過（第 $((i+1)) 次）：$(printf '%s' "$out" | head -c 200)"
                return 0
            fi
        fi
        i=$((i+1)); sleep "${T_HEALTH_INTERVAL:-5}"
    done
    say "健康檢查失敗（試了 $i 次）：$(printf '%s' "${out:-無回應}" | head -c 300)"
    return 1
}

planned_ports() {
    dc config 2>/dev/null | grep -E '^[[:space:]]*published:' \
        | grep -oE '[0-9]+' | sort -u | tr '\n' ' ' | sed 's/ *$//' || true
}
running_ports() {
    local id out=''
    for id in $(dc ps -q 2>/dev/null); do
        out+=$(docker port "$id" 2>/dev/null)$'\n'
    done
    printf '%s' "$out" | grep -oE ':[0-9]+$' | tr -d ':' | sort -u \
        | tr '\n' ' ' | sed 's/ *$//' || true
}
check_ports() {
    [ "${ALLOW_PORT_CHANGE:-0}" = 1 ] && return 0
    local now planned
    now=$(running_ports)
    planned=$(planned_ports)
    if [ -z "$now" ]; then return 0; fi
    if [ -z "$planned" ]; then
        say "（跳過 port 檢查：docker compose config 解析不出 published port）"
        return 0
    fi
    if [ "$now" != "$planned" ]; then
        die "對外 port 會被改掉：現在是【$now】，這次會變成【$planned】。
    容器還沒動，服務不受影響。
    通常是 compose 插值用的變數變了（.env 或 shell 環境裡的 FRONTEND_PORT /
    API_PORT 之類）—— 先確認是不是你要的。
    確定要換就重跑一次並帶著 ALLOW_PORT_CHANGE=1（寫在 .deploy/deploy.env 裡）。"
    fi
    return 0
}

IMAGE_KEEP=${T_IMAGE_KEEP:-5}

repo_of() {
    case "${1##*/}" in
        *:*) printf '%s' "${1%:*}" ;;
        *)   printf '%s' "$1" ;;
    esac
}

deploy_services() {
    printf '%s\n' "$T_SERVICE"
    local spec svc prof
    for spec in ${T_EXTRA_SERVICES:-}; do
        svc=${spec%%:*}; prof=${spec#*:}
        local args=()
        [ -n "$prof" ] && args+=(--profile "$prof")
        if [ -n "$(dc ${args[@]+"${args[@]}"} ps -aq "$svc" 2>/dev/null)" ]; then
            printf '%s\n' "$svc"
        fi
    done
    return 0
}

service_image_ref() {
    local svc=${1:-$T_SERVICE} cid ref f="$STATE_DIR/image-name.${1:-$T_SERVICE}"
    cid=$(dc ps -q "$svc" 2>/dev/null | head -1 || true)
    if [ -n "$cid" ]; then
        ref=$(docker inspect -f '{{.Config.Image}}' "$cid" 2>/dev/null || true)
        if [ -n "$ref" ]; then
            printf '%s\n' "$ref" >"$f"
            printf '%s' "$ref"; return 0
        fi
    fi
    [ -s "$f" ] && { cat "$f"; return 0; }
    return 1
}

TAGS_FILE="$STATE_DIR/image-tags"

remember_tag() {
    local tag=$1 tmp="$TAGS_FILE.tmp"
    { grep -vxF "$tag" "$TAGS_FILE" 2>/dev/null || true; } >"$tmp"
    printf '%s\n' "$tag" >>"$tmp"
    mv "$tmp" "$TAGS_FILE"
}

prune_image_tags() {
    local repo=$1 tag live='' others='' keep tmp="$TAGS_FILE.tmp"
    [ -s "$TAGS_FILE" ] || return 0
    while IFS= read -r tag; do
        [ -n "$tag" ] || continue
        docker image inspect "$tag" >/dev/null 2>&1 || continue
        if [ "$(repo_of "$tag")" = "$repo" ]; then live+="$tag"$'\n'
        else others+="$tag"$'\n'; fi
    done <"$TAGS_FILE"

    keep=$(printf '%s' "$live" | tail -n "$IMAGE_KEEP")
    printf '%s' "$live" | head -n -"$IMAGE_KEEP" | while IFS= read -r tag; do
        [ -n "$tag" ] || continue
        docker rmi "$tag" >/dev/null 2>&1 && say "  輪掉舊標籤 $tag" || true
    done
    { printf '%s' "$others"; printf '%s\n' "$keep"; } >"$tmp"
    mv "$tmp" "$TAGS_FILE"
    return 0
}

snapshot_image() {
    local sha=$1 svc ref repo id n=0
    [ -n "$sha" ] || return 0
    while IFS= read -r svc; do
        [ -n "$svc" ] || continue
        ref=$(service_image_ref "$svc") || continue
        repo=$(repo_of "$ref")
        id=$(docker inspect -f '{{.Id}}' "$ref" 2>/dev/null || true)
        [ -n "$id" ] || { say "（找不到 image $ref，$svc 跳過快照）"; continue; }
        if docker tag "$id" "$repo:sha-${sha:0:12}" 2>/dev/null; then
            say "已保留現行 image：$repo:sha-${sha:0:12}（回滾用，不佔額外空間）"
            remember_tag "$repo:sha-${sha:0:12}"
            prune_image_tags "$repo"
            n=$((n+1))
        else
            say "（$repo 標記失敗，這一版的回滾會走重建那條路）"
        fi
    done < <(deploy_services)
    [ "$n" -gt 0 ] || say "（沒有可快照的執行中容器，這一版的回滾會走重建那條路）"
    return 0
}

rollback_to() {
    local sha=$1 svc ref repo tag ok=1 svcs=''
    git reset --hard "$sha" >/dev/null
    svcs=$(deploy_services)

    while IFS= read -r svc; do
        [ -n "$svc" ] || continue
        ref=$(service_image_ref "$svc") || { ok=0; break; }
        tag="$(repo_of "$ref"):sha-${sha:0:12}"
        if ! docker image inspect "$tag" >/dev/null 2>&1; then
            say "（$svc 沒有 $tag 這個標籤 —— 可能是第一次用這個機制，或已經被輪掉）"
            ok=0; break
        fi
    done <<<"$svcs"

    if [ "$ok" = 1 ]; then
        step "用保留的 image 回滾（不 build、不需要網路）"
        local failed=0
        while IFS= read -r svc; do
            [ -n "$svc" ] || continue
            ref=$(service_image_ref "$svc")
            tag="$(repo_of "$ref"):sha-${sha:0:12}"
            say "  $svc <- $tag"
            docker tag "$tag" "$ref" || { failed=1; break; }
        done <<<"$svcs"
        if [ "$failed" = 0 ] && \
           dc up -d --no-build --force-recreate $svcs; then
            return 0
        fi
        say "（用保留的 image 起不來，改走重建）"
    fi

    step "改用重建回滾（需要連得到套件來源）"
    NEED_BUILD=1
    bring_up
}

SNAPSHOT_SHA=''

bring_up() {
    if [ "$NEED_BUILD" = 1 ] || [ "$NEED_UP" = 1 ]; then check_ports; fi
    if [ "$NEED_BUILD" = 1 ]; then
        snapshot_image "$SNAPSHOT_SHA"
        if [ -n "${T_PREBUILD:-}" ]; then
            step "build 前準備：$T_PREBUILD"
            eval "$T_PREBUILD" || die "準備步驟失敗，沒有動到正在跑的容器"
        fi
        step "重建並啟動 $T_SERVICE"
        dc up --build -d "$T_SERVICE" || die "build/up 失敗，舊容器還在跑（服務沒有中斷）"
    elif [ "$NEED_UP" = 1 ]; then
        step "重新套用 compose 設定"
        dc up -d "$T_SERVICE" || die "up 失敗"
    else
        step "沒有需要重啟的變更，容器維持原狀"
        return 0
    fi

    local spec svc prof
    for spec in ${T_EXTRA_SERVICES:-}; do
        svc=${spec%%:*}; prof=${spec#*:}
        local args=()
        [ -n "$prof" ] && args+=(--profile "$prof")
        if [ -n "$(dc ${args[@]+"${args[@]}"} ps -aq "$svc" 2>/dev/null)" ]; then
            step "順帶重建並更新已存在的 $svc（它可能是另一顆 image，見 Dockerfile 的 target）"
            dc ${args[@]+"${args[@]}"} up --build -d "$svc" || say "⚠ $svc 更新失敗，主服務不受影響"
        else
            say "（$svc 沒有被建立過，跳過 —— 要啟用請先手動 up 一次）"
        fi
    done
}

record() { printf '%s\t%s\t%s\t%s\n' "$(date '+%F %T')" "$1" "$2" "${3:-}" >>"$HISTORY"; }

last_good() {
    [ -f "$HISTORY" ] || return 1
    awk -F'\t' -v cur="$1" '$3=="ok" && $2!=cur {sha=$2} END{if(sha)print sha}' "$HISTORY"
}

MODE=${1:-status}

case "$MODE" in
receive)
    OLD=${2:-}; NEW=${3:-$(git rev-parse HEAD)}
    take_lock
    say "部署 $(basename "$REPO")：$(git log -1 --format='%h %s' "$NEW")"

    if [ -z "$OLD" ] || [ "$OLD" = "0000000000000000000000000000000000000000" ] \
       || ! git cat-file -e "$OLD^{commit}" 2>/dev/null; then
        say "（沒有上一版可比 —— 當成全新部署，完整重建）"
        NEED_BUILD=1; NEED_UP=1
        FILES_IMAGE="  <全部>"$'\n'
    else
        classify < <(git diff --name-only "$OLD" "$NEW")
        if [ "$NEED_BUILD" = 0 ] && [ "$NEED_UP" = 0 ] && [ -z "$FILES_MOUNT" ]; then
            report_plan
            record "$NEW" ok "no-op"
            say ""; say "✓ 只有文件類變更，檔案已更新，容器不用動。"
            exit 0
        fi
    fi

    report_plan
    PREV=$(last_good "$NEW" || true)
    SNAPSHOT_SHA=$OLD
    bring_up

    step "健康檢查"
    if health_ok; then
        record "$NEW" ok "$([ "$NEED_BUILD" = 1 ] && echo rebuild || echo reload)"
        SNAPSHOT_SHA=$NEW; snapshot_image "$NEW"
        say ""; say "✓ 部署完成：$(git log -1 --format='%h %s')"
    else
        record "$NEW" fail ""
        if [ "${AUTO_ROLLBACK:-1}" = 1 ] && [ -n "$PREV" ]; then
            step "⚠ 新版沒過健康檢查 —— 自動回到上一個健康版本 ${PREV:0:8}"
            rollback_to "$PREV"
            if health_ok; then
                record "$PREV" ok "rollback"
                die "新版有問題，已經回到 ${PREV:0:8}，服務正常。伺服器現在跑的是舊版。
    先看 docker compose logs --tail=100 $T_SERVICE 找原因，修好後正常 push 即可
    （修正 commit 是壞版本的後代，deploy 分支照樣 fast-forward）。
    如果是用 --amend 改掉壞的那個 commit，push 要加 --force-history。"
            fi
            record "$PREV" fail "rollback-failed"
            die "回滾後健康檢查還是不過 —— 問題可能不在這次的程式碼。
    看 docker compose logs $T_SERVICE 與 $LOG"
        fi
        die "新版沒過健康檢查（沒有可回滾的版本，或 AUTO_ROLLBACK=0）。
    看 docker compose logs --tail=100 $T_SERVICE"
    fi
    ;;

rebuild)
    take_lock
    say "強制重建（程式碼沒變，通常是 agentkit 或基底 image 換了）"
    NEED_BUILD=1
    SNAPSHOT_SHA=$(git rev-parse HEAD)
    bring_up
    step "健康檢查"
    health_ok || die "重建後健康檢查不過，看 docker compose logs --tail=100 $T_SERVICE"
    record "$(git rev-parse HEAD)" ok "forced-rebuild"
    say ""; say "✓ 重建完成：$(git log -1 --format='%h %s')"
    ;;

rollback)
    take_lock
    CUR=$(git rev-parse HEAD)
    TARGET=${2:-$(last_good "$CUR" || true)}
    [ -n "$TARGET" ] || die "history 裡找不到可以回去的版本，指定 sha：deploy/apply.sh rollback <sha>"
    git cat-file -e "$TARGET^{commit}" 2>/dev/null || die "這台機器上沒有 commit $TARGET"
    say "回滾 ${CUR:0:8} → ${TARGET:0:8}：$(git log -1 --format='%h %s' "$TARGET")"
    SNAPSHOT_SHA=$CUR; snapshot_image "$CUR"
    rollback_to "$TARGET"
    step "健康檢查"
    if health_ok; then record "$TARGET" ok "manual-rollback"; say ""; say "✓ 已回到 ${TARGET:0:8}"
    else record "$TARGET" fail "manual-rollback"; die "回滾後仍不健康，看 logs"; fi
    ;;

status)
    printf '專案      %s\n' "$(basename "$REPO")"
    printf '版本      %s\n' "$(git log -1 --format='%h %s（%cr）' 2>/dev/null || echo '不是 git repo')"
    if ! git diff --quiet 2>/dev/null; then
        printf '⚠ 伺服器上有未提交的修改：\n'; git diff --stat | sed 's/^/  /'
    fi
    printf '\n容器\n'; dc ps -a 2>/dev/null | sed 's/^/  /'
    printf '\n健康檢查\n'
    if out=$(eval "$T_HEALTH_CMD" 2>&1); then printf '  %s\n' "$(printf '%s' "$out" | head -c 400)"
    else printf '  ✗ 打不通：%s\n' "$(printf '%s' "$out" | head -c 200)"; fi
    if [ -f "$HISTORY" ]; then printf '\n最近幾次部署\n'; tail -5 "$HISTORY" | sed 's/^/  /'; fi
    ;;

*)  die "不認得的模式：$MODE（receive|rebuild|rollback|status）" ;;
esac
