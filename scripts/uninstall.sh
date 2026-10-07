#!/usr/bin/env bash
# =============================================================================
# uninstall.sh — 把 Echo 還原成正常的 Amazon Echo
#
# 用法:
#   ./scripts/uninstall.sh --server 192.168.0.212 --echo 192.168.0.142
#   ./scripts/uninstall.sh --echo 192.168.0.142 --full     # 連 /system 也清
#
# 選項:
#   --server IP     Hiyo 大腦（要停掉它的 user units）
#   --echo IP       Echo
#   --key PATH      SSH 私鑰
#   --via ssh|adb   傳輸
#   --full          連 /system/etc/init/hiyo.rc 與 /system/bin/hiyo_boot 一起刪
#   --keep-data     保留 /data/local/tmp 的檔案（只停服務）
#   --dry-run
# =============================================================================
set -uo pipefail

SERVER=""
ECHO=192.168.0.142
SUSER=ac
EKEY="$HOME/.ssh/id_hiyo_echo"
VIA=ssh
FULL=0
KEEP_DATA=0
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --server) SERVER="$2"; shift 2 ;;
    --echo) ECHO="$2"; shift 2 ;;
    --user) SUSER="$2"; shift 2 ;;
    --key) EKEY="$2"; shift 2 ;;
    --via) VIA="$2"; shift 2 ;;
    --full) FULL=1; shift ;;
    --keep-data) KEEP_DATA=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown: $1"; exit 1 ;;
  esac
done

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[0;32m✓\033[0m %s\n' "$*"; }
run()  { if [ "$DRY_RUN" = 1 ]; then echo "    [dry-run] $*"; else "$@"; fi; }

if [ "$VIA" = adb ]; then
  dsh() { adb shell "$*"; }
else
  dsh() { ssh -o BatchMode=yes -i "$EKEY" "root@${ECHO}" "$*"; }
fi

# ── 1. 停 Echo 上的程序，還原 stock ──────────────────────────────────────────
step "1/4 停止 Echo 端程序"
run dsh "killall hiyo_supervisor.sh 2>/dev/null"
run dsh "killall echo_probe echo_control echo_led_watchdog 2>/dev/null"
sleep 2
run dsh "kill -9 \$(pidof echo_probe echo_control echo_led_watchdog) 2>/dev/null"
ok "子程序已停止"

step "2/4 還原 Amazon stock 服務"
run dsh "echo STOCK > /data/local/tmp/echo_led_owner 2>/dev/null"
run dsh "/data/local/tmp/echo-led --restore 2>/dev/null"
run dsh "start mixer"
run dsh "start ledcontroller"
sleep 2
M=$(dsh "getprop init.svc.mixer" 2>/dev/null | tr -d '\r')
[ "$M" = running ] && ok "mixer=running（stock 已還原）" || printf '    ! mixer=%s\n' "$M"
L=$(dsh "getprop init.svc.ledcontroller" 2>/dev/null | tr -d '\r')
[ "$L" = running ] && ok "ledcontroller=running" || printf '    ! ledcontroller=%s\n' "$L"

# ── 3. /system 開機自啟 ──────────────────────────────────────────────────────
if [ "$FULL" = 1 ]; then
  step "3/4 移除 /system 開機自啟"
  if dsh "test -f /system/etc/init/hiyo.rc" 2>/dev/null; then
    # 正常開機時 /system 是唯讀的（system-as-root），無法用 remount 刪除。
    # 必須進 recovery 才能動。這裡只做偵測與提示。
    warn "/system 是唯讀的 — 正常開機時無法刪除 hiyo.rc"
    cat <<'EOM'

      hiyo.rc 還在 /system 裡，必須進 recovery 才能移除：

        adb reboot recovery
        # TWRP → Advanced → Terminal，執行：
        #   mount -o rw,remount /system_root
        #   rm -f /system_root/system/etc/init/hiyo.rc \
        #         /system_root/system/bin/hiyo_boot
        #   umount /system_root
        # 然後 Reboot System

      或使用 --full 產生的移除 ZIP（dist/hiyo-uninit-v1.zip，若存在）。

EOM
  else
    ok "沒有 hiyo.rc（未安裝過）"
  fi
  run dsh "rm -f /data/adb/service.d/hiyo-echo.sh 2>/dev/null"
  ok "Magisk service.d 已清（若有）"
else
  step "3/4 略過 /system（--full 可啟用）"
fi

# ── 4. 資料 ──────────────────────────────────────────────────────────────────
if [ "$KEEP_DATA" = 1 ]; then
  step "4/4 保留 /data/local/tmp（--keep-data）"
else
  step "4/4 清除 /data/local/tmp 的 Hiyo 檔案"
  for f in echo_probe echo_control echo_led_watchdog hiyo_supervisor.sh echo-led echo-cast \
           hiyo_supervisor.log echo_led_owner echo_led_hb d2a_hiyo_boot.new d2a_hiyo_rc.new; do
    run dsh "rm -f /data/local/tmp/$f"
  done
  ok "已清除"
fi

# ── 5. Server 端 ─────────────────────────────────────────────────────────────
if [ -n "$SERVER" ]; then
  step "5/5 停用 Server 端 units"
  S="ssh -o BatchMode=yes ${SUSER}@${SERVER}"
  for u in hermes-echo-tts hermes-echo-bridge hermes-echo-gadget hermes-echo-turn hermes-echo-stt hermes-echo-ha-media; do
    run $S "systemctl --user disable --now $u 2>/dev/null" && ok "$u 已停用"
  done
  run $S "rm -f ~/.config/systemd/user/hermes-echo-*.service"
  run $S "rm -rf ~/.config/systemd/user/hermes-echo-*.service.d"
  run $S "systemctl --user daemon-reload"
  ok "units 已移除（~/voice/echo-bridge 保留，要刪請自行 rm -rf）"
  printf '    ! Hermes 本体 (seed-vc / gateway / dashboard) 未動\n'
else
  step "5/5 略過 Server 端（--server 可啟用）"
fi

cat <<'EOF'

還原完成。

  建議: 重新開機 Echo，確認它回到正常的 Amazon Echo 行為。
        adb reboot   /   ssh root@<echo> reboot
EOF
