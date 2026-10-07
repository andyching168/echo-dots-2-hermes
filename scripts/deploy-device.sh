#!/usr/bin/env bash
# =============================================================================
# deploy-device.sh — 把 Hiyo stack 推上一台剛刷好機的 Echo Dot 2
#
# 支援兩種傳輸：
#   ssh  (預設)  — Echo 已開 dropbear 且有金鑰
#   adb          — 只有 USB / adb 可用（--via adb）
#
# 用法:
#   ./scripts/deploy-device.sh --host 192.168.0.142 --server 192.168.0.212
#   ./scripts/deploy-device.sh --via adb --server 192.168.0.212
#   ./scripts/deploy-device.sh --host 192.168.0.142 --enable-autostart recovery
#   ./scripts/deploy-device.sh --host 192.168.0.142 --enable-autostart host-driven
#
# 選項:
#   --host H             Echo IP (預設 192.168.0.142)
#   --via ssh|adb        傳輸方式 (預設 ssh)
#   --key PATH          SSH 私鑰 (預設 ~/.ssh/id_hiyo_echo)
#   --server IP         Hiyo 大腦位址（寫入 supervisor）
#   --enable-autostart MODE
#         none          不設開機自啟（預設；重開機後要重跑）
#         recovery      產生 recovery 用的 ZIP 並推到 /sdcard（唯一能寫
#                       /system 的方法）
#         host-driven   產生主機端 systemd unit（不動裝置）
#   --flash-ors         搭配 recovery 模式：寫 /cache/recovery/command 並
#                       自動重開進 recovery，讓 TWRP 自動刷入（免手動）
#   --pull-back         先把裝置上的現況備份到 device/backup-<date>/
#   --dry-run           只印出要做的事
#
# 註: 舊的 init / magisk 模式已移除。實測確認：
#     * 正常開機時 /system 是唯讀的 (system-as-root, /dev/root ro)，
#       mount -o rw,remount /system 無效。
#     * Magisk v27 無法 patch 本機 boot image (LZMA ramdisk)，
#       所以沒有 /data/adb/service.d/。
#     唯一可行路徑是 recovery + TWRP ZIP。詳見 docs/QUICKSTART.md §5。
# =============================================================================
set -euo pipefail

HOST=192.168.0.142
VIA=ssh
KEY="$HOME/.ssh/id_hiyo_echo"
SERVER=192.168.0.212
AUTOSTART=none
PULL_BACK=0
DRY_RUN=0
FLASH_ORS=0

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP=/data/local/tmp

while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --via) VIA="$2"; shift 2 ;;
    --key) KEY="$2"; shift 2 ;;
    --server) SERVER="$2"; shift 2 ;;
    --enable-autostart) AUTOSTART="$2"; shift 2 ;;
    --flash-ors) FLASH_ORS=1; shift ;;
    --pull-back) PULL_BACK=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[0;32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[0;33m!\033[0m %s\n' "$*"; }
die()  { printf '    \033[0;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
run()  { if [ "$DRY_RUN" = 1 ]; then echo "    [dry-run] $*"; else "$@"; fi; }

# ── transport ────────────────────────────────────────────────────────────────
if [ "$VIA" = adb ]; then
  command -v adb >/dev/null || die "找不到 adb"
  dsh() { adb shell "$*"; }
  dpush() { adb push "$1" "$2" >/dev/null; }
  dcat() { adb shell "cat '$1'"; }
else
  SSH_OPTS=(-o ConnectTimeout=10 -o BatchMode=yes -o StrictHostKeyChecking=accept-new)
  [ -f "$KEY" ] && SSH_OPTS+=(-i "$KEY")
  dsh() { ssh "${SSH_OPTS[@]}" "root@${HOST}" "$*"; }
  dpush() {
    # Echo 的 dropbear 沒有 sftp-server，scp 會失敗 → 用 stdin 串流
    ssh "${SSH_OPTS[@]}" "root@${HOST}" "cat > '$2'" < "$1"
  }
  dcat() { ssh "${SSH_OPTS[@]}" "root@${HOST}" "cat '$1'"; }
fi

step "目標 Echo: ${VIA}://${HOST}   大腦: ${SERVER}"
[ -d "$HERE/device/bin" ] || die "找不到 $HERE/device/bin"

# ── 0. 前置檢查 ──────────────────────────────────────────────────────────────
step "0/6 前置檢查"
dsh "true" >/dev/null 2>&1 || die "連不上 Echo（${VIA}）"
ID=$(dsh "id" 2>/dev/null | tr -d '\r')
echo "$ID" | grep -q 'uid=0' || die "不是 root：$ID"
ok "root shell OK"
MODEL=$(dsh "getprop ro.product.model" | tr -d '\r')
REL=$(dsh "getprop ro.build.version.release" | tr -d '\r')
ARCH=$(dsh "uname -m" | tr -d '\r')
ok "model=$MODEL  android=$REL  arch=$ARCH"
case "$ARCH" in armv7l|armv8l) ok "架構相符";; *) warn "非 armv7l（本包二進位是 ARMv7）";; esac

# ── 1. 備份現況 ──────────────────────────────────────────────────────────────
if [ "$PULL_BACK" = 1 ]; then
  step "1/6 備份裝置現況"
  BK="$HERE/device/backup-$(date +%Y%m%d_%H%M%S)"
  mkdir -p "$BK"
  for f in echo_probe echo_control echo_led_watchdog hiyo_supervisor.sh echo-led echo-cast; do
    dsh "test -f $TMP/$f" 2>/dev/null && { dcat "$TMP/$f" > "$BK/$f" 2>/dev/null; ok "backed up $f"; } || true
  done
  dsh "test -f /system/etc/init/hiyo.rc" 2>/dev/null && dcat /system/etc/init/hiyo.rc > "$BK/hiyo.rc" 2>/dev/null || true
  ok "備份於 $BK"
else
  step "1/6 跳過備份（--pull-back 可啟用）"
fi

# ── 2. 推送二進位 ────────────────────────────────────────────────────────────
step "2/6 推送執行檔 → $TMP"
run dsh "mkdir -p $TMP"
for b in echo_probe echo_control echo_led_watchdog; do
  run dpush "$HERE/device/bin/$b" "$TMP/$b"
  run dsh "chmod 755 $TMP/$b"
  ok "$b"
done

# ── 3. 推送腳本 ──────────────────────────────────────────────────────────────
step "3/6 推送裝置端腳本"
for s in hiyo_supervisor.sh echo-led echo-cast; do
  [ -f "$HERE/device/scripts/$s" ] || { warn "缺 $s，跳過"; continue; }
  run dpush "$HERE/device/scripts/$s" "$TMP/$s"
  run dsh "chmod 755 $TMP/$s"
  ok "$s"
done
# supervisor 內的 HOST 改成目標 server
if [ "$DRY_RUN" = 0 ]; then
  dsh "sed -i 's|^HOST=.*|HOST=${SERVER}|' $TMP/hiyo_supervisor.sh"
  ok "supervisor HOST → ${SERVER}"
fi

# ── 4. 開機自啟 ──────────────────────────────────────────────────────────────
step "4/6 開機自啟: $AUTOSTART"
case "$AUTOSTART" in
  none)
    warn "不設定；重開機後需重跑本腳本（或手動起 supervisor）"
    ;;
  recovery)
    # 唯一能寫 /system 的路徑：包成 TWRP ZIP，從 recovery 刷入。
    # 正常開機時 /system 是唯讀的（system-as-root），remount 無效。
    if dsh "test -f /system/etc/init/hiyo.rc" 2>/dev/null; then
      ok "hiyo.rc 已存在（先前已刷入）"
    fi
    ZIP="$HERE/dist/hiyo-init-v1.zip"
    if [ ! -f "$ZIP" ]; then
      warn "找不到 $ZIP — 先執行 ./scripts/make-init-zip.sh"
      run "$HERE/scripts/make-init-zip.sh"
    fi
    [ -f "$ZIP" ] || die "無法產生 ZIP"
    run dpush "$ZIP" "/sdcard/hiyo-init-v1.zip"
    ok "ZIP 已推到 /sdcard/hiyo-init-v1.zip"

    # 驗證推送完整性（推壞了進 recovery 就白跑）
    if [ "$DRY_RUN" = 0 ]; then
      LOCAL_MD5=$(md5sum "$ZIP" | cut -d' ' -f1)
      REMOTE_MD5=$(dsh "md5sum /sdcard/hiyo-init-v1.zip 2>/dev/null" | tr -d '\r' | cut -d' ' -f1)
      if [ "$LOCAL_MD5" = "$REMOTE_MD5" ]; then
        ok "推送完整性 OK ($LOCAL_MD5)"
      else
        die "推送損毀！local=$LOCAL_MD5 remote=$REMOTE_MD5 — 重新推一次"
      fi
    fi

    if [ "$FLASH_ORS" = 1 ]; then
      # ORS：寫 /cache/recovery/command 後重開進 recovery，TWRP 會自動刷
      step "觸發 ORS 自動刷入"
      run dsh "echo '--update_package=/sdcard/hiyo-init-v1.zip' > /cache/recovery/command"
      ok "/cache/recovery/command 已寫入"
      run dsh "sync"
      warn "即將重開進 recovery —— TWRP 會自動刷入 ZIP"
      run dsh "reboot recovery"
      echo
      printf '    \033[1;33m等約 60-90 秒，TWRP 刷完後會停在 recovery。\033[0m\n'
      printf '    \033[1;33m然後執行：\033[0m\n'
      printf '      adb reboot\n'
      printf '      %s/scripts/verify.sh --server %s --echo %s\n' "$HERE" "$SERVER" "$HOST"
    else
      echo
      printf '    \033[1;33m接下來要進 recovery 刷入（無法從 SSH 遙控）：\033[0m\n'
      cat <<EOM

      推薦 — ORS 自動刷（本機是 headless TWRP，看不到畫面，這是唯一實際可用的方式）:
        adb shell 'echo "--update_package=/sdcard/hiyo-init-v1.zip" > /cache/recovery/command'
        adb reboot recovery
        # 等 60-90 秒讓 TWRP 跑完，然後 adb reboot

      或讓本腳本代勞（會自動重開）:
        $0 --host $HOST --server $SERVER --enable-autostart recovery --flash-ors

      刷完後驗證:
        ssh root@$HOST 'md5sum /system/etc/init/hiyo.rc /system/bin/hiyo_boot'
        # 期望 1b3fc2ff... hiyo.rc / 9df0f29f... hiyo_boot

      完整逐步說明: docs/RECOVERY_GUIDE.md
EOM
    fi
    ;;
  host-driven)
    U="$HERE/device/host-driven/hiyo-echo-bootstrap.service"
    mkdir -p "$HERE/device/host-driven"
    cat > "$U" <<EOF
[Unit]
Description=Hiyo Echo device bootstrap (host-driven)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=%h/rs03qr/v2/echo-dots-2-hermes/scripts/deploy-device.sh --host ${HOST} --server ${SERVER} --via ssh
ExecStop=/usr/bin/ssh -o BatchMode=yes root@${HOST} 'killall echo_probe echo_control echo_led_watchdog; start mixer'

[Install]
WantedBy=default.target
EOF
    ok "已產生 $U"
    warn "在主機上安裝:  cp $U ~/.config/systemd/user/ && systemctl --user enable --now hiyo-echo-bootstrap"
    ;;
  *) die "未知 autostart 模式: $AUTOSTART" ;;
esac

# ── 5. 環境準備 ──────────────────────────────────────────────────────────────
step "5/6 裝置端環境"
# supervisor 需要這些（init 不會設）
run dsh "setprop ANDROID_ROOT /system 2>/dev/null || true"
run dsh "setprop ANDROID_DATA /data 2>/dev/null || true"
# LED owner 預設 STOCK
run dsh "echo STOCK > $TMP/echo_led_owner 2>/dev/null || true"
ok "環境變數與 LED owner 就緒"

# 停掉既有 supervisor，避免兩個同時跑（dry-run 不動作）
if [ "$DRY_RUN" = 0 ]; then
  dsh "killall hiyo_supervisor.sh 2>/dev/null; killall echo_probe echo_control echo_led_watchdog 2>/dev/null" || true
  sleep 2
fi

# ── 6. 啟動並驗證 ────────────────────────────────────────────────────────────
step "6/6 啟動 supervisor"
if [ "$DRY_RUN" = 1 ]; then
  echo "    [dry-run] 略過啟動與驗證"
  echo
  printf '\033[1;32mDry-run 完成 — 沒有對裝置做任何修改。\033[0m\n'
  exit 0
fi

run dsh "nohup $TMP/hiyo_supervisor.sh >/dev/null 2>&1 &"
sleep 6

echo
for p in echo_probe echo_control echo_led_watchdog; do
  PID=$(dsh "pidof $p" 2>/dev/null | tr -d '\r')
  if [ -n "$PID" ]; then ok "$p running (pid $PID)"
  else warn "$p 沒起來 — 看 $TMP/hiyo_supervisor.log"; fi
done
MIX=$(dsh "getprop init.svc.mixer" 2>/dev/null | tr -d '\r')
ok "mixer=$MIX"

cat <<EOF

\033[1;32m裝置部署完成。\033[0m

  日誌:   ssh root@${HOST} 'tail -f $TMP/hiyo_supervisor.log'
  狀態:   ssh root@${HOST} 'for p in echo_probe echo_control echo_led_watchdog; do echo "\$p: \$(pidof \$p)"; done'
  停止:   ssh root@${HOST} 'killall hiyo_supervisor.sh; start mixer'
  驗證:   $HERE/scripts/verify.sh --server ${SERVER} --echo ${HOST}
EOF
