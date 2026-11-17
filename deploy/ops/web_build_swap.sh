#!/usr/bin/env bash
# web_build_swap.sh -- 前端「另目錄 build、成功後才換入、健康檢查失敗自動回退」。
#
# 背景（RUNBOOK §4.2、附錄 B 2026-11-15）：舊流程在線上目錄原地 `npm run build`，
# `next build` 一開始就清掉舊 .next，build 期間（與 build 失敗後）線上頁面的 JS/CSS 全回 400。
# 本腳本改成：在 BUILD_DIR 底下「本次執行專屬」的 run-<時間戳>-<PID>/ 工作目錄（原始碼副本）build；
# 成功才 rename 換入 WEB_DIR/.next；重啟 + 健康檢查；失敗自動換回 .next-prev。
#
# 在正式機以 ubuntu 執行（不是 root）：需要 root 的動作走 ${SUDO} 前綴。
#   NEXT_PUBLIC_SITE_ORIGIN=https://trade.filet.app deploy/ops/web_build_swap.sh [--force] [--allow-api-start]
#   deploy/ops/web_build_swap.sh --rollback [--force] [--allow-api-start]
#
# 旗標：--force            無視維護旗標（MAINTENANCE_FLAG）
#       --allow-api-start  API_UNIT 非 active 也照跑（重啟 filet-dashboard 會經 Wants= 把它拉起來）
#       --rollback         不 build：把 WEB_DIR/.next-prev 換回 .next 並重啟、健康檢查。
#                          .next-prev 的 BUILD_ID 或 static 不存在／為空就拒絕（不動任何東西）；成功後 .next-prev 即不存在。
#                          WEB_DIR/.next 缺席時走復原模式（直接 .next-prev -> .next，不碰 .next-failed）。
#                          唯一支援的手動回退方式——不要自己 mv（.next-prev 不在時會把線上 .next 搬走）。
#
# BUILD_DIR 護欄（腳本會對它 sudo chown -R、清理其中舊的 run-*、並把原始碼同步進本次的 run-*；指錯位置會把整棵樹交給 ubuntu 並刪掉
# 其中名為 run-* 的東西，所以任何動作之前先擋）：
#   以實體路徑（解析符號連結）判斷：不得是 /、不得等於 WEB_DIR、不得是 WEB_DIR 的上層、不得在 WEB_DIR 之內；
#   且只操作「自己建立的目錄」——不存在就建立並寫標記檔 .web_build_swap_dir；已存在的空目錄也寫標記檔；
#   已存在、非空、沒有標記檔 → 拒絕。
#
# 訊號模型（兩個階段，沒有第三種）：
#   可中斷階段＝前置檢查、準備 BUILD_DIR、build、驗證產物：INT/TERM/HUP 照常結束腳本，不影響線上。
#     build 會被「盡力」終止：有 setsid 時 build 在新 session，對整個行程群組送 TERM；沒有（macOS）只終止直接子行程。
#     殘留的 build 行程不影響任何後續執行的產物——每次執行用獨立的 run-* 工作目錄，殘留行程只會寫它自己的目錄。
#   不可中斷階段＝從「第一個會改動 WEB_DIR 的指令之前」到腳本結束（換入、重啟、健康檢查、自動回退、最終狀態報告；
#     --rollback 則從第一個 rename 之前起）：INT/TERM/HUP/PIPE 一律忽略、不再恢復，子行程繼承。
#     所有輸出都吞掉寫入錯誤（終端機不在了也不會結束），並同時附加寫入 LOG_FILE。
#
# 結束碼＝腳本結束時的「實際最終狀態」（由檔案系統實況決定，不只靠流程變數）：
#   0   一般：.next 是新版、已重啟、健康檢查通過、.next-prev 是舊版；--rollback：.next 是原 .next-prev 的版本、已重啟、健康檢查通過
#   1   線上的 .next 未變、服務未重啟；若在換入步驟中止，更早的 .next-prev 可能已被移除（--rollback 的前置檢查會如實拒絕）
#   2   用法錯誤，未動任何東西
#   4   一般：新版曾換入但失敗，已自動回退——.next 是舊版、已重啟、健康檢查通過；失敗的新版在 .next-failed
#   5   其他一切，需要人處理（.next 缺席、回退的 rename 失敗、回退後重啟或健康檢查失敗、--rollback 的重啟或健康檢查失敗…）
#   129/130/143  只可能發生在可中斷階段（HUP/INT/TERM）：WEB_DIR 未被改動（語意同 1）
#   141          保留碼：輸出一律吞掉寫入錯誤（可中斷階段也一樣，終端機不在了不會結束腳本），正常不會出現
# 每次結束都印「最終狀態報告」（stdout 或 stderr，並寫入 LOG_FILE），含下一步該執行的確切指令。
# 刪除 .next-prev／.next-failed 一律「先 rename 成 <名稱>.deleting-<PID> 再刪」：刪一半的殘骸永遠不會頂著會被當成回退來源的名字。
#
# 環境變數（全部可覆寫，供離線測試；預設值見下）。除 SUDO 外，空字串＝使用預設值。
#   WEB_DIR(/opt/filet/spark/web) BUILD_DIR(/opt/filet/web-build，在部署樹之外) NEXT_PUBLIC_SITE_ORIGIN(必填) SUDO NPM_BIN RESTART_CMD HEALTH_URL
#   HEALTH_TIMEOUT_S MAINTENANCE_FLAG API_UNIT
#   以下四個是 plan 變數表之外為測試/可攜性加的：CURL_BIN SYSTEMCTL_BIN LOCK_FILE LOG_FILE
#   LOG_FILE 預設用 mktemp 產生（權限 0600）；無法建立或寫入 → 前置檢查階段直接失敗（結束碼 1），不降級。
set -Eeuo pipefail

WEB_DIR="${WEB_DIR:-/opt/filet/spark/web}"
# BUILD_DIR 刻意放在部署樹 /opt/filet/spark 之外：它是 ubuntu 所有的工作副本，放在樹內會打破
# RUNBOOK §3.2「樹內（var/ 除外）沒有非 root 檔案」的不變量。必須與 WEB_DIR 同一個檔案系統。
BUILD_DIR="${BUILD_DIR:-/opt/filet/web-build}"
SUDO="${SUDO-sudo}" # 空字串是合法值（測試：不用 sudo）
NPM_BIN="${NPM_BIN:-npm}"
RESTART_CMD="${RESTART_CMD:-sudo systemctl restart filet-dashboard}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:3000/}"
HEALTH_TIMEOUT_S="${HEALTH_TIMEOUT_S:-30}"
MAINTENANCE_FLAG="${MAINTENANCE_FLAG:-/var/www/maintenance/ON}"
API_UNIT="${API_UNIT:-filet-api}"
CURL_BIN="${CURL_BIN:-curl}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
LOCK_FILE="${LOCK_FILE:-/run/lock/filet-web-build-swap.lock}"
LOG_FILE="${LOG_FILE:-$(mktemp "${TMPDIR:-/tmp}/web_build_swap.log.XXXXXX" 2>/dev/null || echo "")}"

BUILD_MARKER=".web_build_swap_dir"
BUILD_DIR_STATE="" # new / empty / ours（guard_build_dir 設定）
FORCE=0
ALLOW_API_START=0
ROLLBACK=0
REPORT=1
FAIL_CODE=1
LOCK_DIR_HELD=""
TMP_DIR=""
HEALTH_REASON=""
BUILD_PID=""
BUILD_GROUP=0 # build 以 setsid 啟動（對 -PID 行程群組送訊號）
BUILD_SECS="?"
RUN_DIR=""        # 本次執行專屬的 build 工作目錄 BUILD_DIR/run-<UTC>-<PID>
RUN_DIR_NOTE="未建立"

# 最終狀態所需的事實（由流程更新；結束碼另外以檔案系統實況驗證）
UNINT=0           # 已進入不可中斷階段
MUTATED=0         # 已開始改動 WEB_DIR
OLD_ID=""         # 執行前 .next 的 BUILD_ID（一般模式）
NEW_ID=""         # 新 build 的 BUILD_ID（一般模式）
PREV_ID0=""       # --rollback 開始時 .next-prev 的 BUILD_ID
SWAP_DONE=0       # 新版已換入且所有換入指令都成功
SWAP_ABORTED=0    # 換入中途失敗並已還原／未動
ROLLBACK_TRIED=0  # 嘗試過回退
FILES_ROLLED=0    # 回退的檔案層已完成（.next 是 .next-prev 的版本）
RECOVERED=0       # 回退走了「.next 缺席 -> 由 .next-prev 復原」模式
SVC_ATTEMPTED=0   # 換入／回退之後嘗試過重啟
START_ID=""       # 執行開始時 .next 的 BUILD_ID（＝當時正在服務的版本）
SVC_BID=""        # 最後一次「成功重啟」當下 .next 的 BUILD_ID；沒有成功重啟過則為空。只在成功重啟時更新，從不被清除
RESTART_LAST="none" # 最近一次重啟「嘗試」的結果：none（未嘗試）／ok／fail。報告據此決定「正在服務的版本」能不能說、怎麼說
ERR_LINE=""       # ERR trap 只記錄（行號與指令）；on_exit 在需要時才印
ERR_CMD=""
ERROR_PRINTED=0   # 已有 die 印過 ERROR 訊息
USAGE_ERROR=0     # 參數／環境變數用法錯誤（結束碼 2）：未做任何檢查與改動；報告僅列出目錄現況，不得對服務行程、線上狀態或是否需要回退做任何斷言
HEALTH_BID=""     # 最後一次健康檢查「通過」當下 .next 的 BUILD_ID
HEALTH_STATE="未執行"
RC_FINAL=1

# ---------------------------------------------------------------- 輸出
# 所有輸出都附加寫入 LOG_FILE；寫入失敗（讀端已關閉、磁碟錯誤）一律吞掉，不讓腳本結束——
# 可中斷與不可中斷兩個階段都一樣（寫終端機的動作在子 shell 裡，被 SIGPIPE 殺掉的只是子 shell），所以 141 是保留碼。
emit() { # $1=out|err  $2=文字
  printf '%s\n' "$2" >>"${LOG_FILE}" 2>/dev/null || true
  # 寫到終端機的部分放進子 shell：bash 的 printf 內建在寫入失敗（EPIPE）後會把自己的 stdout 關掉，
  # fd 1 隨即被下一次 open 重用（輸出會跑進別的檔案、$(...) 的內容被污染）。子 shell 吞掉這個副作用，
  # 子 shell 被 SIGPIPE 殺掉也只是它自己；主行程永遠不會因為輸出而結束。
  if [ "$1" = err ]; then
    (printf '%s\n' "$2" >&2 2>/dev/null) || true
  else
    (printf '%s\n' "$2" 2>/dev/null) || true
  fi
}
say() { emit out "[web_build_swap] $*"; }
warn() { emit err "[web_build_swap] $*"; }
die() {
  ERROR_PRINTED=1
  warn "ERROR: $*"
  exit "${FAIL_CODE}"
}
usage_die() {
  USAGE_ERROR=1
  FAIL_CODE=2
  die "$@"
}

# ---------------------------------------------------------------- 不可中斷階段（單一入口）
# 此函式是整支腳本唯一設定「忽略訊號」的地方，且一經進入不再恢復。
# 進入後也關閉 set -e：這個階段的每個指令都明確處理自己的失敗，不靠「失敗就結束」。
# $1=quiet 時不印提示（on_exit 內用，確保結束處理本身也不會被訊號打斷）。
enter_uninterruptible() {
  if [ "${UNINT}" -eq 1 ]; then return 0; fi
  trap '' INT TERM HUP PIPE
  trap - ERR # 這個階段的失敗都是預期內、逐一處理；不要讓 ERR trap 對它們喊「未預期」
  UNINT=1
  set +e
  if [ "${1:-}" != quiet ]; then
    local n
    if [ "${ROLLBACK}" -eq 1 ]; then
      n=$((HEALTH_TIMEOUT_S + 90))
    else
      n=$((2 * HEALTH_TIMEOUT_S + 180))
    fi
    say "從這裡開始不可中斷（INT／TERM／HUP／PIPE 一律忽略，直到腳本結束），最長約 ${n} 秒；請不要關掉終端機以外的任何東西，結果見最終狀態報告與 ${LOG_FILE}"
  fi
  return 0
}

# ---------------------------------------------------------------- 檔案系統實況
# BUILD_ID（或「不存在」）。
bid() {
  if [ -s "$1/BUILD_ID" ]; then
    cat "$1/BUILD_ID" 2>/dev/null || echo "?"
  elif [ -e "$1" ]; then
    echo "(存在但沒有 BUILD_ID)"
  else
    echo "不存在"
  fi
}
# 完整的 bundle：BUILD_ID 非空且 static/ 非空（與步驟 4 的產物驗證同一判準）。
valid_bundle() {
  [ -s "$1/BUILD_ID" ] && [ -d "$1/static" ] && [ -n "$(ls -A "$1/static" 2>/dev/null)" ]
}

# ---------------------------------------------------------------- 重啟與健康檢查
# 重啟輸出用 $(...) 收集後再寫 log：不在 LOG_FILE 上做重導向，log 出任何問題都不會讓重啟指令根本沒跑。
# SVC_BID 只在重啟「成功」時更新，從不清除——報告據此如實說明「最後一次成功重啟的是哪一版」。
do_restart() {
  local rout rrc line
  SVC_ATTEMPTED=1
  rout=$(bash -c "${RESTART_CMD}" 9>&- 2>&1)
  rrc=$?
  if [ -n "${rout}" ]; then
    while IFS= read -r line; do emit out "[restart] ${line}"; done <<EOF_RESTART_OUT
${rout}
EOF_RESTART_OUT
  fi
  if [ "${rrc}" -eq 0 ]; then
    RESTART_LAST=ok
    SVC_BID=$(bid "${WEB_DIR}/.next")
    return 0
  fi
  RESTART_LAST=fail
  return 1
}

# 回 0＝首頁 200 且首頁引用的每個 /_next/static JS/CSS 皆 200；回 1 並設 HEALTH_REASON。
# 首頁輪詢與逐一檢查靜態資源共用同一個總時限（HEALTH_TIMEOUT_S），每次 curl 的 --max-time 不超過剩餘時間。
check_health() {
  local deadline code body origin assets asset n=0 mt
  deadline=$((SECONDS + HEALTH_TIMEOUT_S))
  body="${TMP_DIR}/health.html"
  while :; do
    mt=$((deadline - SECONDS))
    [ "${mt}" -gt 5 ] && mt=5
    [ "${mt}" -lt 1 ] && mt=1
    code=$("${CURL_BIN}" -sS -o "${body}" -w '%{http_code}' --max-time "${mt}" "${HEALTH_URL}" 2>/dev/null) || code=000
    [ "${code}" = 200 ] && break
    if [ "${SECONDS}" -ge "${deadline}" ]; then
      HEALTH_REASON="${HEALTH_URL} 在 ${HEALTH_TIMEOUT_S}s 內未回 200（最後狀態碼 ${code}）"
      return 1
    fi
    sleep 1
  done
  origin=$(printf '%s' "${HEALTH_URL}" | sed -E 's#^([A-Za-z]+://[^/]+).*#\1#')
  assets=$(grep -Eo '/_next/static/[^"'"'"' <>\\]+\.(js|css)' "${body}" | sort -u || true)
  if [ -z "${assets}" ]; then
    HEALTH_REASON="首頁 200 但 HTML 內找不到任何 /_next/static 的 JS/CSS 引用"
    return 1
  fi
  for asset in ${assets}; do
    if [ "${SECONDS}" -ge "${deadline}" ]; then
      HEALTH_REASON="逐一檢查靜態資源時超過總時限 ${HEALTH_TIMEOUT_S}s（已檢查 ${n} 個，其餘未檢查）"
      return 1
    fi
    mt=$((deadline - SECONDS))
    [ "${mt}" -gt 10 ] && mt=10
    [ "${mt}" -lt 1 ] && mt=1
    code=$("${CURL_BIN}" -sS -o /dev/null -w '%{http_code}' --max-time "${mt}" "${origin}${asset}" 2>/dev/null) || code=000
    if [ "${code}" != 200 ]; then
      HEALTH_REASON="靜態資源 ${origin}${asset} 回 ${code}（首頁 HTML 引用的資源必須全部 200）"
      return 1
    fi
    n=$((n + 1))
  done
  say "健康檢查通過：首頁 200，${n} 個靜態資源皆 200"
  return 0
}

do_health() {
  if check_health; then
    HEALTH_BID=$(bid "${WEB_DIR}/.next")
    HEALTH_STATE="通過（${HEALTH_BID}）"
    return 0
  fi
  HEALTH_STATE="失敗（當時 .next=$(bid "${WEB_DIR}/.next")）：${HEALTH_REASON}"
  return 1
}

# ---------------------------------------------------------------- 換入與回退（只用 rename；每步以檔案系統實況驗證，不靠指令回傳碼）
# retire_dir <路徑>：「刪除前先失效」——先 rename 成 <路徑>.deleting-<PID> 再刪。
#   回 0＝原名已不存在（本來就沒有，或 rename 成功；之後 rm 失敗只留下垃圾目錄，它不是回退來源，印警告、流程繼續，下次執行會清掉）
#   回 1＝rename 失敗，原目錄完好（呼叫端必須中止該步驟）
retire_dir() {
  local d="$1" trash="$1.deleting-$$"
  if [ ! -e "${d}" ] && [ ! -L "${d}" ]; then return 0; fi
  ${SUDO} mv "${d}" "${trash}"
  if [ -e "${d}" ] || [ -L "${d}" ]; then return 1; fi
  ${SUDO} rm -rf "${trash}"
  if [ -e "${trash}" ]; then
    warn "刪除 ${trash} 失敗（只留下垃圾目錄：它不是回退來源，下次執行會清掉）"
  fi
  return 0
}

# swap_in 回傳：0＝新版已換入且全部指令成功；1＝中途失敗且 .next 是舊版（已還原或根本沒動）；
#               2＝某指令回報失敗、但檔案系統實況顯示新版已在 .next（→ 走自動回退）；3＝無法收拾（→ 5）。
# 絕不在目的地已存在時執行 mv（否則 mv 會把來源搬進該目錄裡面）。
swap_in() {
  MUTATED=1
  local failed=0 cur
  if ! retire_dir "${WEB_DIR}/.next-prev"; then
    warn "無法把舊的 ${WEB_DIR}/.next-prev 改名移開（rename 失敗），換入中止；.next 與 .next-prev 都原封不動"
    return 1
  fi
  ${SUDO} mv "${WEB_DIR}/.next" "${WEB_DIR}/.next-prev" || failed=1
  if [ -e "${WEB_DIR}/.next" ]; then
    warn "mv .next .next-prev 沒有生效（.next 仍在），換入中止"
    return 1
  fi
  ${SUDO} mv "${RUN_DIR}/.next" "${WEB_DIR}/.next" || failed=1
  cur=$(bid "${WEB_DIR}/.next")
  if [ "${failed}" -eq 0 ] && [ "${cur}" = "${NEW_ID}" ]; then
    return 0
  fi
  if [ "${cur}" = "${NEW_ID}" ]; then
    warn "換入指令回報失敗，但檔案系統實況顯示新版（${NEW_ID}）已在 ${WEB_DIR}/.next：視為已換入，改走自動回退"
    return 2
  fi
  if [ ! -e "${WEB_DIR}/.next" ] && valid_bundle "${WEB_DIR}/.next-prev"; then
    warn "換入中途失敗：${WEB_DIR}/.next 不存在，由 .next-prev 還原"
    ${SUDO} mv "${WEB_DIR}/.next-prev" "${WEB_DIR}/.next" || true
    if [ "$(bid "${WEB_DIR}/.next")" = "${OLD_ID}" ]; then
      return 1
    fi
    return 3
  fi
  if [ "${cur}" = "${OLD_ID}" ]; then return 1; fi
  return 3
}

# swap_back：.next-prev 換回 .next。回 0＝檔案層完成（.next 是 .next-prev 的版本）；非零＝失敗。
#   .next 存在 -> 舊 .next-failed 刪除、.next -> .next-failed、.next-prev -> .next
#   .next 缺席 -> 復原模式：不碰 .next-failed，直接 .next-prev -> .next
#   任何情況下，先確認 .next-prev 是完整 bundle，才動任何東西（含刪除既有的 .next-failed——那是排查證據）。
swap_back() {
  local prev_id
  RECOVERED=0
  if ! valid_bundle "${WEB_DIR}/.next-prev"; then
    warn "無法回退：${WEB_DIR}/.next-prev 不是完整的 bundle（BUILD_ID 或 static 不存在／為空）；未動任何東西"
    return 1
  fi
  prev_id=$(bid "${WEB_DIR}/.next-prev")
  ROLLBACK_TRIED=1
  MUTATED=1
  if [ -e "${WEB_DIR}/.next" ]; then
    say "回退：.next -> .next-failed，.next-prev -> .next"
    # 已確認 .next-prev 有效之後才動 .next-failed；且先 rename 成 .deleting-<PID> 再刪（刪一半的殘骸不會頂著 .next-failed 的名字）
    if ! retire_dir "${WEB_DIR}/.next-failed"; then
      warn "無法把舊的 ${WEB_DIR}/.next-failed 改名移開（rename 失敗），回退中止；.next 未動、.next-prev 原封不動"
      return 1
    fi
    ${SUDO} mv "${WEB_DIR}/.next" "${WEB_DIR}/.next-failed"
    if [ -e "${WEB_DIR}/.next" ]; then
      warn "mv .next .next-failed 沒有生效（.next 仍在），回退中止"
      return 1
    fi
    ${SUDO} mv "${WEB_DIR}/.next-prev" "${WEB_DIR}/.next"
  else
    RECOVERED=1
    say "復原模式：${WEB_DIR}/.next 不存在，由 .next-prev 復原（不碰 .next-failed）"
    ${SUDO} mv "${WEB_DIR}/.next-prev" "${WEB_DIR}/.next"
  fi
  if [ ! -e "${WEB_DIR}/.next" ]; then
    # 指令回報失敗而 .next 仍缺席：重試一次；還是不行——非復原模式才把剛搬走的那一版（.next-failed）放回去，至少 .next 不缺席。
    # 復原模式（原本就沒有 .next）下 .next-failed 是排查證據、不是 .next：絕不搬它；復原失敗就如實以 .next 缺席結束（結束碼 5）。
    if valid_bundle "${WEB_DIR}/.next-prev"; then
      ${SUDO} mv "${WEB_DIR}/.next-prev" "${WEB_DIR}/.next"
    fi
    if [ "${RECOVERED}" -eq 0 ] && [ ! -e "${WEB_DIR}/.next" ] && [ -e "${WEB_DIR}/.next-failed" ]; then
      ${SUDO} mv "${WEB_DIR}/.next-failed" "${WEB_DIR}/.next"
    fi
  fi
  # 檔案層完成＝.next 是 .next-prev 的版本，且 .next-prev 已不存在（BUILD_ID 相同時不得偽陽性）
  if [ "$(bid "${WEB_DIR}/.next")" = "${prev_id}" ] && [ ! -e "${WEB_DIR}/.next-prev" ]; then
    FILES_ROLLED=1
    return 0
  fi
  return 1
}

# 自動回退：換入後只要不是「成功走完」就走這裡（檔案層回退＋重啟＋健康檢查）。
auto_rollback() {
  warn "!!!!!! 換入後流程未成功完成（$1），自動回退 !!!!!!"
  if ! swap_back; then
    warn "!!!!!!!!!! 回退失敗：檔案層沒有回到舊版（見最終狀態報告）!!!!!!!!!!"
    return 1
  fi
  warn "已回退到舊版（BUILD_ID=$(bid "${WEB_DIR}/.next")）。失敗的新版留在 ${WEB_DIR}/.next-failed 供排查。"
  if ! do_restart; then
    warn "!!!!!! 回退後重啟失敗：檔案已是舊版，只差重啟（見最終狀態報告）!!!!!!"
    return 1
  fi
  if do_health; then
    warn "回退後健康檢查通過：線上為舊版。"
    return 0
  fi
  warn "!!!!!! 回退後健康檢查仍失敗：${HEALTH_REASON} !!!!!!"
  return 1
}

# ---------------------------------------------------------------- 結束碼與最終狀態報告
# 由「檔案系統實況＋流程事實」決定結束碼（RC_FINAL），而不是沿用流程中最後一個失敗碼。
# 目前 .next 的版本是否「已被成功重啟」且「健康檢查通過」（以 BUILD_ID 比對，不靠會被清掉的旗標）。
svc_matches_files() { [ -n "${SVC_BID}" ] && [ "${SVC_BID}" = "$(bid "${WEB_DIR}/.next")" ]; }
health_matches_files() { [ -n "${HEALTH_BID}" ] && [ "${HEALTH_BID}" = "$(bid "${WEB_DIR}/.next")" ]; }

decide_code() {
  local cur failed ok=0
  cur=$(bid "${WEB_DIR}/.next")
  failed=$(bid "${WEB_DIR}/.next-failed")
  RC_FINAL=5
  if svc_matches_files && health_matches_files; then ok=1; fi
  if [ "${ROLLBACK}" -eq 1 ]; then
    if [ "${FILES_ROLLED}" -eq 1 ] && [ "${cur}" = "${PREV_ID0}" ] && [ "${ok}" -eq 1 ]; then
      RC_FINAL=0
    fi
    return 0
  fi
  if [ "${SWAP_DONE}" -eq 1 ] && [ "${ROLLBACK_TRIED}" -eq 0 ] && [ "${cur}" = "${NEW_ID}" ] && [ "${ok}" -eq 1 ]; then
    RC_FINAL=0
  elif [ "${FILES_ROLLED}" -eq 1 ] && [ "${cur}" = "${OLD_ID}" ] && [ "${failed}" = "${NEW_ID}" ] && [ "${ok}" -eq 1 ]; then
    RC_FINAL=4
  elif [ "${SWAP_ABORTED}" -eq 1 ] && [ "${cur}" = "${OLD_ID}" ] && [ "${SVC_ATTEMPTED}" -eq 0 ]; then
    RC_FINAL=1
  fi
  return 0
}

final_report() {
  local rc="$1" cur prev failed meaning svc_line consistent serving nextstep line out_kind=out extra="" mode_text
  if [ "${USAGE_ERROR}" -eq 1 ]; then
    mode_text="未定（參數錯誤）"
  elif [ "${ROLLBACK}" -eq 1 ]; then
    mode_text="--rollback"
  else
    mode_text="部署"
  fi
  cur=$(bid "${WEB_DIR}/.next")
  prev=$(bid "${WEB_DIR}/.next-prev")
  failed=$(bid "${WEB_DIR}/.next-failed")
  case "${rc}" in
    0) meaning="成功" ;;
    1) meaning="線上的 .next 未變、服務未重啟；若在換入步驟中止，更早的 .next-prev 可能已被移除（--rollback 的前置檢查會如實拒絕）" ;;
    2) meaning="用法錯誤，未動任何東西" ;;
    4) meaning="新版換入後失敗，已自動回退到舊版並通過健康檢查" ;;
    5) meaning="需要人處理（見下一步）" ;;
    129 | 130 | 141 | 143) meaning="在可中斷階段被訊號中斷：WEB_DIR 未被改動" ;;
    *) meaning="未預期的結束碼" ;;
  esac
  # 報告只陳述腳本「確實知道」的事；不知道就寫「未知」並說怎麼查，不推測。
  if [ -n "${SVC_BID}" ]; then
    svc_line="${SVC_BID}"
  else
    svc_line="本次執行未成功重啟過"
  fi
  case "${RESTART_LAST}" in
    ok)
      # 最近一次重啟嘗試成功：比較當時的 BUILD_ID 與目前 .next
      if [ "${SVC_BID}" = "${cur}" ]; then
        consistent="一致（檔案與正在服務的都是 ${cur}）"
      else
        consistent="不一致——檔案已變更但尚未重啟（檔案是 ${cur}，最後一次成功重啟時是 ${SVC_BID}）"
      fi
      ;;
    fail)
      # 最近一次重啟嘗試失敗：服務多半已停；不能說一致，也不能斷言正在服務的是哪一版
      consistent="未知——最近一次重啟指令失敗，服務可能已停止；以 systemctl is-active filet-dashboard 與健康檢查（curl -s -o /dev/null -w '%{http_code}' ${HEALTH_URL}）為準"
      ;;
    *)
      # 本次執行未嘗試重啟
      consistent="服務未被本次執行重啟（仍是執行前啟動的行程）"
      if [ -n "${START_ID}" ] && [ "${cur}" != "${START_ID}" ]; then
        consistent="${consistent}；檔案已變更但尚未重啟（檔案是 ${cur}，執行開始時是 ${START_ID}）"
      fi
      ;;
  esac
  if [ "${USAGE_ERROR}" -eq 1 ]; then
    # 用法錯誤：腳本未做任何檢查與改動，兩行關於服務行程的陳述都不是腳本知道的事
    svc_line="（參數錯誤，未檢查）"
    consistent="（參數錯誤，未檢查）"
  fi
  # 下一步：只印「確切指令」，依檔案系統實況挑一種
  if [ "${USAGE_ERROR}" -eq 1 ]; then
    # 用法錯誤：只能說這件事——不對線上狀態、是否需要回退做任何斷言
    nextstep="參數錯誤，未動任何東西。修正參數後重跑（bash $0 --help 看用法）。"
  elif [ "${rc}" -eq 0 ]; then
    if [ "${ROLLBACK}" -eq 1 ]; then
      nextstep="無（已回退完成；.next-prev 已被消耗，無法再次 --rollback，要再回退請走整包重部署，RUNBOOK §9.3）"
    else
      nextstep="無。事後發現新版有問題時：bash $0 --rollback"
    fi
  elif [ "${rc}" -eq 129 ] || [ "${rc}" -eq 130 ] || [ "${rc}" -eq 143 ]; then
    # 只可能發生在可中斷階段（未改動 WEB_DIR）；兩種模式共用；不提 ERROR（沒有對應的 ERROR 訊息）
    nextstep="被訊號中斷，線上未受影響；直接重跑即可。"
  elif [ "${rc}" -eq 1 ] || [ "${MUTATED}" -eq 0 ]; then
    if [ "${ROLLBACK}" -eq 1 ]; then
      # --rollback 被拒絕或失敗而 WEB_DIR 未動：回退「沒有執行」，要人依上面的 ERROR 處理後重跑 --rollback
      nextstep="回退沒有執行，線上未變（.next 仍是 ${cur}）。依上面 ERROR 的指示處理後重跑 bash $0 --rollback：維護旗標 → 確認後加 --force；${API_UNIT} 非 active → 確認後加 --allow-api-start；另一個執行持有鎖 → 等它結束；.next-prev 不是完整版本 → 沒有可回退的版本，走整包重部署（RUNBOOK §9.3）。"
    else
      nextstep="線上未變（.next 仍是 ${cur}）；排除上面的 ERROR／警告原因後直接重跑。不要執行 --rollback：沒有任何東西需要回退（--rollback 只用於結束碼 5 且 .next 缺席，或你事後自行判斷新版有問題）。"
    fi
  elif [ ! -e "${WEB_DIR}/.next" ] && valid_bundle "${WEB_DIR}/.next-prev"; then
    nextstep="bash $0 --rollback    （${WEB_DIR}/.next 不存在，但舊版完好在 .next-prev：復原模式會由 .next-prev 復原並重啟；不要自己 mv）"
  elif [ "${FILES_ROLLED}" -eq 1 ] && ! svc_matches_files; then
    nextstep="${RESTART_CMD}    （檔案已是舊版 ${cur}，只差重啟；重啟後用 curl -s -o /dev/null -w '%{http_code}' ${HEALTH_URL} 確認 200）"
  elif [ "${ROLLBACK_TRIED}" -eq 0 ] || [ "${FILES_ROLLED}" -eq 0 ]; then
    if [ "${ROLLBACK}" -eq 0 ] && [ "${cur}" = "${NEW_ID}" ] && ! valid_bundle "${WEB_DIR}/.next-prev"; then
      nextstep="整包重部署（RUNBOOK §9.3）：.next-prev 不是完整 bundle，無法回退，.next 是新版 ${cur}。"
    else
      nextstep="目前線上 .next 的 BUILD_ID=${cur}。檢查 ${LOG_FILE}、sudo journalctl -u filet-dashboard -n 100、curl -s -o /dev/null -w '%{http_code}' ${HEALTH_URL}；確認後再決定是否 bash $0 --rollback。"
    fi
  else
    nextstep="目前線上 .next 的 BUILD_ID=${cur}。檢查 ${LOG_FILE}、sudo journalctl -u filet-dashboard -n 100、curl -s -o /dev/null -w '%{http_code}' ${HEALTH_URL}。"
  fi
  if [ "${rc}" -ne 0 ]; then out_kind=err; fi
  if [ "${rc}" -eq 0 ] && [ "${ROLLBACK}" -eq 0 ]; then
    extra="舊版保留於  : ${WEB_DIR}/.next-prev（只在換入成功之後、下一次 RUNBOOK §3.2 部署／下一次腳本執行／任何回退之前存在）
手動回退    : bash $0 --rollback（只在 .next-prev 還在時有效；不要自己 mv——.next-prev 不在時會把線上 .next 搬走）"
  fi
  while IFS= read -r line; do
    emit "${out_kind}" "${line}"
  done <<EOF_REPORT
==================== web_build_swap 最終狀態報告 ====================
模式        : ${mode_text}
結束碼      : ${rc}（${meaning}）
.next       : ${cur}
.next-prev  : ${prev}
.next-failed: ${failed}
最後一次成功重啟：${svc_line}
目前 .next 與正在服務的版本：${consistent}
健康檢查    : ${HEALTH_STATE}
舊 BUILD_ID : ${OLD_ID:-（未執行）}
新 BUILD_ID : ${NEW_ID:-（未產生）}
build 秒數  : ${BUILD_SECS}s
build 工作目錄: ${RUN_DIR:-（未建立）}（${RUN_DIR_NOTE}）
build log   : ${LOG_FILE}
下一步      : ${nextstep}
${extra}
=====================================================================
EOF_REPORT
}

on_exit() {
  local rc=$?
  enter_uninterruptible quiet # 結束處理本身（回報、清理）也不可被訊號或 PIPE 打斷
  if [ "${MUTATED}" -eq 1 ]; then
    decide_code
    rc="${RC_FINAL}"
  fi
  # ERR trap 只記錄；這裡在「結束碼非 0、且沒有任何 die 印過 ERROR」時才印出那一筆（set -e 靜默結束的路徑），
  # 所以 $(...) 內函式的正常失敗（之後由呼叫端的 die 處理）不會多印一行誤導的 ERROR。
  if [ "${rc}" -ne 0 ] && [ "${ERROR_PRINTED}" -eq 0 ] && [ -n "${ERR_LINE}" ]; then
    warn "ERROR：未預期的指令失敗（第 ${ERR_LINE} 行：${ERR_CMD}）"
  fi
  if [ "${REPORT}" -eq 1 ]; then
    final_report "${rc}"
  fi
  if [ -n "${LOCK_DIR_HELD}" ]; then rmdir "${LOCK_DIR_HELD}" 2>/dev/null; fi
  if [ -n "${TMP_DIR}" ]; then rm -rf "${TMP_DIR}"; fi
  exit "${rc}"
}

# 可中斷階段的訊號處理：「盡力」終止進行中的 build、不動線上、以訊號碼結束。
# 有 setsid 時 build 自成一個行程群組，對整個群組送 TERM；沒有就只終止直接子行程。不等它結束——
# 正確性不靠這一步：殘留的 build 行程只會寫它自己的 run-* 目錄，結構上混不進後續執行的產物。
kill_build() {
  if [ -z "${BUILD_PID}" ]; then return 0; fi
  if [ "${BUILD_GROUP}" -eq 1 ]; then
    kill -TERM -- "-${BUILD_PID}" 2>/dev/null || kill -TERM "${BUILD_PID}" 2>/dev/null || true
  else
    kill "${BUILD_PID}" 2>/dev/null || true
  fi
  return 0
}
on_signal() {
  kill_build
  exit "$1"
}
# ERR trap 只記錄行號與指令（不直接印）：可中斷階段因 set -e 結束時，由 on_exit 印出對應的 ERROR 訊息
# （不可中斷階段會關掉它）。子 shell／$(...) 內的失敗只記在子 shell 自己的變數，不會污染主行程。
on_err() {
  ERR_LINE="$1"
  ERR_CMD="$2"
}
trap on_exit EXIT
trap 'on_err "$LINENO" "$BASH_COMMAND"' ERR
trap 'on_signal 130' INT
trap 'on_signal 143' TERM
trap 'on_signal 129' HUP

# ---------------------------------------------------------------- BUILD_DIR 護欄
# 實體絕對路徑（解析符號連結）；路徑尚不存在時，解析最近的既有祖先再接上剩餘部分。不是目錄 → 回非零。
phys_path() {
  local p="$1" tail="" base
  case "${p}" in /*) ;; *) p="${PWD}/${p}" ;; esac
  while [ ! -e "${p}" ] && [ ! -L "${p}" ] && [ "${p}" != / ]; do
    base=$(basename "${p}")
    tail="/${base}${tail}"
    p=$(dirname "${p}")
  done
  (cd "${p}" 2>/dev/null && printf '%s%s\n' "$(pwd -P)" "${tail}")
}

# 全部以實體路徑判斷；通過後把 WEB_DIR／BUILD_DIR 改寫成實體路徑（後續動作不再經過符號連結）。
guard_build_dir() {
  local pw pb listing
  case "/${WEB_DIR}/" in */../* | */./*) die "WEB_DIR 不得含 . 或 .. 路徑成分：${WEB_DIR}" ;; esac
  case "/${BUILD_DIR}/" in */../* | */./*) die "BUILD_DIR 不得含 . 或 .. 路徑成分：${BUILD_DIR}" ;; esac
  pw=$(phys_path "${WEB_DIR}") && [ -n "${pw}" ] || die "無法解析 WEB_DIR 的實體路徑：${WEB_DIR}"
  pb=$(phys_path "${BUILD_DIR}") && [ -n "${pb}" ] || die "無法解析 BUILD_DIR 的實體路徑（它的某一層不是目錄？）：${BUILD_DIR}"
  [ "${pb}" != / ] || die "BUILD_DIR 不得是 /（會對整個檔案系統 sudo chown -R 並清理其中的 run-*）"
  [ "${pb}" != "${pw}" ] || die "BUILD_DIR 不得等於 WEB_DIR（實體路徑相同：${pw}）；未動任何東西"
  case "${pw}/" in
    "${pb}"/*) die "BUILD_DIR（${pb}）是 WEB_DIR（${pw}）的上層目錄：腳本會對上層整棵樹 sudo chown -R 並刪除其中名為 run-* 的東西；未動任何東西" ;;
  esac
  case "${pb}/" in
    "${pw}"/*) die "BUILD_DIR（${pb}）位於 WEB_DIR（${pw}）之內；未動任何東西" ;;
  esac
  if [ ! -e "${pb}" ]; then
    BUILD_DIR_STATE=new
  elif [ ! -d "${pb}" ]; then
    die "BUILD_DIR（${pb}）存在但不是目錄；未動任何東西"
  elif [ -f "${pb}/${BUILD_MARKER}" ] && [ ! -L "${pb}/${BUILD_MARKER}" ]; then
    BUILD_DIR_STATE=ours
  else
    listing=$(ls -A "${pb}") || die "無法列出 BUILD_DIR（${pb}）內容；未動任何東西"
    if [ -z "${listing}" ]; then
      BUILD_DIR_STATE=empty
    else
      die "BUILD_DIR（${pb}）已有內容、卻沒有標記檔 ${BUILD_MARKER}：這不是本腳本建立的目錄，拒絕以 sudo chown -R 與清理 run-* 操作它。確認是誤設就改正 BUILD_DIR；確定要沿用就自己清空它；未動任何東西"
    fi
  fi
  WEB_DIR="${pw}"
  BUILD_DIR="${pb}"
}

# ---------------------------------------------------------------- 引數
for arg in "$@"; do
  case "${arg}" in
    --force) FORCE=1 ;;
    --allow-api-start) ALLOW_API_START=1 ;;
    --rollback) ROLLBACK=1 ;;
    -h | --help)
      REPORT=0
      sed -n '/^set -Eeuo pipefail/q;2,$p' "$0"
      exit 0
      ;;
    *)
      usage_die "未知參數：${arg}（可用：--force --allow-api-start --rollback；--help 看用法）"
      ;;
  esac
done
# HEALTH_TIMEOUT_S 正規化成十進位整數（去除前導零——否則 08 會被算術當成八進位而出錯、健康檢查被跳過），
# 且必須在 1–600 之間（打錯成 3000 會讓失敗路徑不可中斷約 100 分鐘）；非數字、0、超出範圍一律拒絕（結束碼 2）。
# 先去前導零、以「最多 4 位數字」的字串長度擋下會溢位的值，才做十進位轉換。空字串視為未設（沿用預設 30，與其他變數一致）。
case "${HEALTH_TIMEOUT_S}" in '' | *[!0-9]*) usage_die "HEALTH_TIMEOUT_S 必須是 1–600 之間的整數秒：${HEALTH_TIMEOUT_S}" ;; esac
hts="${HEALTH_TIMEOUT_S}"
while [ "${#hts}" -gt 1 ] && [ "${hts#0}" != "${hts}" ]; do hts="${hts#0}"; done
[ "${#hts}" -le 4 ] || usage_die "HEALTH_TIMEOUT_S 超出允許範圍 1–600 秒：${HEALTH_TIMEOUT_S}"
HEALTH_TIMEOUT_S=$((10#${hts}))
{ [ "${HEALTH_TIMEOUT_S}" -ge 1 ] && [ "${HEALTH_TIMEOUT_S}" -le 600 ]; } ||
  usage_die "HEALTH_TIMEOUT_S 超出允許範圍 1–600 秒：${HEALTH_TIMEOUT_S}"

# ---------------------------------------------------------------- 步驟 1：前置檢查（可中斷階段）
# LOG_FILE 必須能建立與寫入：不能就在前置檢查階段直接失敗（結束碼 1、未動任何東西），不靜默降級。
if [ -z "${LOG_FILE}" ] || ! { : >>"${LOG_FILE}"; } 2>/dev/null; then
  die "LOG_FILE 無法建立或寫入（${LOG_FILE:-mktemp 失敗，TMPDIR=${TMPDIR:-/tmp}}）：最終狀態報告與 build 輸出都要落檔，請改 LOG_FILE 或修正權限；未動任何東西"
fi
say "步驟 1/9 前置檢查"
# --rollback：沒有可回退的版本就拒絕，且什麼都不動。
if [ "${ROLLBACK}" -eq 1 ]; then
  valid_bundle "${WEB_DIR}/.next-prev" ||
    die "沒有可回退的版本（${WEB_DIR}/.next-prev 的 BUILD_ID 或 static 不存在／為空；腳本自動回退後、或 RUNBOOK §3.2 的 rsync 之後它本來就不在）。請走整包重部署（RUNBOOK §9.3）；未動任何東西"
else
  [ -n "${NEXT_PUBLIC_SITE_ORIGIN:-}" ] || die "NEXT_PUBLIC_SITE_ORIGIN 未設定（它會被烤進 bundle，沒有預設值；正式機為 https://trade.filet.app）"
  export NEXT_PUBLIC_SITE_ORIGIN
  [ -f "${WEB_DIR}/.next/BUILD_ID" ] || die "${WEB_DIR}/.next/BUILD_ID 不存在：線上沒有可回退的版本，本腳本不處理首次部署（走 RUNBOOK §4.2 的舊流程）"
  # 線上 .next 必須是完整版本（valid_bundle：BUILD_ID 非空且 static/ 非空，與回退來源同一判準）：
  # 否則新版健康檢查失敗時自動回退無從開始、壞版會留在線上，所以一開始就不部署（結束碼 1、未動任何東西）。
  valid_bundle "${WEB_DIR}/.next" ||
    die "線上 ${WEB_DIR}/.next 不是完整版本（BUILD_ID 或 static/ 不存在／為空），無法作為回退來源；本腳本不處理這種狀態，走 RUNBOOK §4.2 舊流程或 §9.3；未動任何東西"
  [ -d "${WEB_DIR}/node_modules" ] || die "${WEB_DIR}/node_modules 不存在（build 要靠符號連結借用它；先照 RUNBOOK §4.2 跑 npm ci）"
  guard_build_dir # 必須在任何會動到 BUILD_DIR 的 sudo／chown／rsync／rm 之前（步驟 2 之前）
fi
if [ -e "${MAINTENANCE_FLAG}" ] && [ "${FORCE}" -ne 1 ]; then
  die "維護旗標存在（${MAINTENANCE_FLAG}）：維護中不部署前端；確定要做加 --force"
fi
if [ "${ROLLBACK}" -ne 1 ]; then
  command -v rsync >/dev/null 2>&1 || die "找不到 rsync"
  command -v "${NPM_BIN}" >/dev/null 2>&1 || die "找不到 NPM_BIN=${NPM_BIN}（nvm/PATH？）"
fi
command -v "${CURL_BIN}" >/dev/null 2>&1 || die "找不到 CURL_BIN=${CURL_BIN}"

# 單一執行鎖：優先 flock；沒有 flock（例如 macOS）退回原子的 mkdir 鎖。
if command -v flock >/dev/null 2>&1; then
  { exec 9>"${LOCK_FILE}"; } 2>/dev/null || die "無法開啟鎖檔 ${LOCK_FILE}"
  flock -n 9 || die "另一個 web_build_swap 正在執行（鎖 ${LOCK_FILE}）"
else
  mkdir "${LOCK_FILE}.d" 2>/dev/null || die "另一個 web_build_swap 正在執行，或殘留的鎖目錄 ${LOCK_FILE}.d（確認沒有行程後刪除）"
  LOCK_DIR_HELD="${LOCK_FILE}.d"
fi

if [ "${ALLOW_API_START}" -ne 1 ]; then
  "${SYSTEMCTL_BIN}" is-active --quiet "${API_UNIT}" ||
    die "${API_UNIT} 目前不是 active：重啟 filet-dashboard 會經 Wants= 把它拉起來（2026-10-04 事故）；確定要這樣加 --allow-api-start"
fi
TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/web_build_swap.XXXXXX") || die "mktemp 失敗"
START_ID=$(bid "${WEB_DIR}/.next") # 執行開始時正在服務的版本（報告用）

# ---------------------------------------------------------------- --rollback 模式（不 build、不碰 BUILD_DIR）
if [ "${ROLLBACK}" -eq 1 ]; then
  PREV_ID0=$(bid "${WEB_DIR}/.next-prev")
  say "--rollback：.next（$(bid "${WEB_DIR}/.next")）-> ${PREV_ID0}"
  enter_uninterruptible # 在第一個會改動 WEB_DIR 的指令（swap_back 內）之前
  if swap_back; then
    if [ "${RECOVERED}" -eq 1 ]; then say "線上原本沒有 .next，已由 .next-prev 復原（.next-failed 原封不動）"; fi
    if do_restart; then
      if do_health; then
        :
      else
        warn "回退後健康檢查失敗：${HEALTH_REASON}"
      fi
    else
      warn "回退後重啟失敗：檔案已是 ${PREV_ID0}，只差重啟（見最終狀態報告）"
    fi
  else
    warn "回退失敗（見最終狀態報告）"
    if [ "${MUTATED}" -eq 0 ]; then exit 1; fi
  fi
  exit 0 # 結束碼由 on_exit 依最終狀態決定
fi

# ---------------------------------------------------------------- 步驟 2：準備 BUILD_DIR 與本次專屬的 run-* 工作目錄
say "步驟 2/9 準備 ${BUILD_DIR}（本次專屬 run-* 工作目錄：原始碼副本＋node_modules 符號連結＋.next/cache）"
dev_of() { stat -c %d "$1" 2>/dev/null || stat -f %d "$1"; }
# 護欄（guard_build_dir）已在步驟 1 通過；這裡才第一次動到 BUILD_DIR：先建立／認領，再清理、chown -R、建立 run-*。
if [ "${BUILD_DIR_STATE}" = new ]; then
  ${SUDO} mkdir -p "${BUILD_DIR}" || die "無法建立 ${BUILD_DIR}"
fi
if [ "${BUILD_DIR_STATE}" != ours ]; then
  # 新建或空目錄：只 chown 目錄本身（內容為空），再寫標記檔——之後的清理／chown -R 才有資格動它。標記檔永遠在 BUILD_DIR 根目錄。
  if [ -n "${SUDO}" ]; then
    ${SUDO} chown "$(id -u):$(id -g)" "${BUILD_DIR}" || die "無法把 ${BUILD_DIR} 交給 $(id -un)"
  fi
  : >"${BUILD_DIR}/${BUILD_MARKER}" || die "無法寫入標記檔 ${BUILD_DIR}/${BUILD_MARKER}"
fi
# 清掉先前執行留下的 run-*（刪不乾淨不算失敗：殘留行程可能還在寫，下次再清；殘留行程也混不進本次產物）
# 用 ${SUDO}（先前換入中止時留下的 run-*/.next 可能已是 root 擁有）；run-* 若是符號連結，只移除連結本身、不跟隨。
for old_run in "${BUILD_DIR}"/run-*; do
  [ -e "${old_run}" ] || [ -L "${old_run}" ] || continue
  if [ -L "${old_run}" ]; then
    ${SUDO} rm -f "${old_run}" 2>/dev/null || true
  else
    ${SUDO} rm -rf "${old_run}" 2>/dev/null || true
  fi
  if [ -e "${old_run}" ] || [ -L "${old_run}" ]; then
    warn "舊的 build 工作目錄 ${old_run} 沒有刪乾淨；下次執行再清（它只會是它自己的目錄，不影響本次產物）"
  fi
done
# 清掉 WEB_DIR 內殘留的 .deleting-* 垃圾目錄（先前 rename 成功、刪除失敗的殘骸；刪不掉只警告）
for trash in "${WEB_DIR}"/.next-prev.deleting-* "${WEB_DIR}"/.next-failed.deleting-*; do
  [ -e "${trash}" ] || continue
  ${SUDO} rm -rf "${trash}" 2>/dev/null || true
  if [ -e "${trash}" ]; then warn "垃圾目錄 ${trash} 刪不掉（它不是回退來源）；下次執行再清"; fi
done
if [ -n "${SUDO}" ]; then
  # chown -R 不碰符號連結目標：先把殘留 run-* 內的 node_modules 符號連結拆掉
  find "${BUILD_DIR}" -maxdepth 2 -type l -name node_modules -exec rm -f {} + 2>/dev/null || true
  ${SUDO} chown -R "$(id -u):$(id -g)" "${BUILD_DIR}" || die "無法把 ${BUILD_DIR} 交給 $(id -un)"
fi
[ "$(dev_of "${WEB_DIR}")" = "$(dev_of "${BUILD_DIR}")" ] ||
  die "${WEB_DIR} 與 ${BUILD_DIR} 不在同一個檔案系統：.next 換入不是原子 rename（BUILD_DIR 必須與 WEB_DIR 同一磁碟）"
# 本次執行專屬的工作目錄：任何先前執行殘留的行程只會寫它自己的 run-* 目錄，結構上混不進本次產物。
RUN_DIR="${BUILD_DIR}/run-$(date -u +%Y%m%dT%H%M%SZ)-$$"
RUN_DIR_NOTE="保留供排查"
mkdir "${RUN_DIR}" || die "無法建立工作目錄 ${RUN_DIR}"
# 原始碼同步到本次工作目錄；排除 WEB_DIR 內所有 .next* 目錄（.next、.next-prev、.next-failed、*.deleting-*）與 node_modules
rsync -rlpt --chmod=u+rwX --exclude node_modules --exclude '/.next*' \
  "${WEB_DIR}"/ "${RUN_DIR}"/ || die "同步原始碼到 ${RUN_DIR} 失敗（工作目錄保留供排查）"
ln -sfn "${WEB_DIR}/node_modules" "${RUN_DIR}/node_modules" || die "無法建立 node_modules 符號連結"
if [ -d "${WEB_DIR}/.next/cache" ]; then
  mkdir -p "${RUN_DIR}/.next" && cp -R "${WEB_DIR}/.next/cache" "${RUN_DIR}/.next/cache" || die "複製 .next/cache 失敗"
fi

# ---------------------------------------------------------------- 步驟 3：build（可中斷：訊號會終止 build，線上不受影響）
say "步驟 3/9 在 ${RUN_DIR} 執行 ${NPM_BIN} run build（log：${LOG_FILE}）"
BUILD_T0=$(date +%s)
# 背景執行＋wait：訊號能立刻打斷 wait。9>&- 讓 build 子行程不繼承 flock 的 fd 9。
# 有 setsid 時 build 自成 session／行程群組，收到訊號時對整個群組送 TERM（盡力而為；macOS 沒有 setsid，只終止直接子行程）。
if command -v setsid >/dev/null 2>&1; then
  BUILD_GROUP=1
  (cd "${RUN_DIR}" && exec setsid "${NPM_BIN}" run build) 9>&- >>"${LOG_FILE}" 2>&1 &
else
  (cd "${RUN_DIR}" && exec "${NPM_BIN}" run build) 9>&- >>"${LOG_FILE}" 2>&1 &
fi
BUILD_PID=$!
if wait "${BUILD_PID}"; then BUILD_RC=0; else BUILD_RC=$?; fi
BUILD_PID=""
if [ "${BUILD_RC}" -ne 0 ]; then
  warn "---- build log 末 30 行（完整：${LOG_FILE}）----"
  tail -n 30 "${LOG_FILE}" >&2 || true
  die "build 失敗；線上版未被改動（${WEB_DIR}/.next 原封不動）；工作目錄保留供排查：${RUN_DIR}"
fi
BUILD_SECS=$(($(date +%s) - BUILD_T0))

# ---------------------------------------------------------------- 步驟 4：驗證產物
say "步驟 4/9 驗證產物"
[ -s "${RUN_DIR}/.next/BUILD_ID" ] || die "build 回報成功但 ${RUN_DIR}/.next/BUILD_ID 不存在或為空；線上版未被改動"
[ -d "${RUN_DIR}/.next/static" ] && [ -n "$(ls -A "${RUN_DIR}/.next/static" 2>/dev/null)" ] ||
  die "build 回報成功但 ${RUN_DIR}/.next/static 不存在或為空；線上版未被改動"
OLD_ID=$(cat "${WEB_DIR}/.next/BUILD_ID")
NEW_ID=$(cat "${RUN_DIR}/.next/BUILD_ID")

# ---------------------------------------------------------------- 步驟 5：換入前的最後準備（仍只動 BUILD_DIR）
say "步驟 5/9 換入：${OLD_ID} -> ${NEW_ID}"
# 先在本次工作目錄內把新 .next 設成 root 唯讀（RUNBOOK §4.4 的安全前提），失敗時線上仍未被動到。
# a+rX 防 ubuntu 的 umask 把檔案收成 dashboard 使用者讀不到。
if [ -n "${SUDO}" ]; then
  ${SUDO} chown -R root:root "${RUN_DIR}/.next" || die "chown root:root 失敗；線上版未被改動"
fi
${SUDO} chmod -R go-w,a+rX "${RUN_DIR}/.next" || die "chmod 失敗；線上版未被改動"

# ---------------------------------------------------------------- 不可中斷階段：換入、重啟、健康檢查、自動回退、報告
enter_uninterruptible # 在第一個會改動 WEB_DIR 的指令（swap_in 內的 rm）之前
swap_in
SWAP_RC=$?
NEED_ROLLBACK=""
case "${SWAP_RC}" in
  0) SWAP_DONE=1 ;;
  1)
    SWAP_ABORTED=1
    warn "換入中止：.next 仍是舊版 ${OLD_ID}，服務未重啟（結束碼 1）"
    ;;
  2) NEED_ROLLBACK="換入指令回報失敗" ;;
  *) warn "!!!!!! 換入中途失敗且無法收拾（.next=$(bid "${WEB_DIR}/.next")）!!!!!!" ;;
esac
# 新 .next 已被 rename 走（換入成功，或指令回報失敗但實況顯示已換入）：本次工作目錄沒有用了，刪除；
# 失敗（rc 1／3，.next 沒有換入）則保留供排查，路徑寫在最終狀態報告。刪不乾淨只警告。
if [ "${SWAP_RC}" -eq 0 ] || [ "${SWAP_RC}" -eq 2 ]; then
  rm -rf "${RUN_DIR}" 2>/dev/null
  if [ -e "${RUN_DIR}" ]; then
    RUN_DIR_NOTE="刪除失敗，殘留 ${RUN_DIR}（下次執行會清）"
    warn "本次 build 工作目錄 ${RUN_DIR} 沒有刪乾淨（下次執行會清）"
  else
    RUN_DIR_NOTE="已刪除（新 .next 已換入線上）"
  fi
fi
if [ "${SWAP_DONE}" -eq 1 ]; then
  say "步驟 6/9 重啟：${RESTART_CMD}"
  if do_restart; then
    say "步驟 7/9 健康檢查：${HEALTH_URL}（最多 ${HEALTH_TIMEOUT_S}s）"
    if ! do_health; then
      warn "健康檢查失敗：${HEALTH_REASON}"
      NEED_ROLLBACK="健康檢查失敗"
    fi
  else
    warn "重啟指令失敗（見 ${LOG_FILE}）"
    NEED_ROLLBACK="重啟指令失敗"
  fi
fi
if [ -n "${NEED_ROLLBACK}" ]; then
  SWAP_DONE=0
  auto_rollback "${NEED_ROLLBACK}"
fi
say "步驟 9/9 結束（結束碼與最終狀態由 on_exit 依檔案系統實況決定）"
exit 0
