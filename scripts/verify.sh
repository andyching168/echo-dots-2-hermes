#!/usr/bin/env bash
# =============================================================================
# verify.sh — 兩端健康檢查
#
# 用法:
#   ./scripts/verify.sh --server 192.168.0.212 --echo 192.168.0.142
#   ./scripts/verify.sh --server localhost
#   ./scripts/verify.sh --server 192.168.0.212 --echo 192.168.0.142 --watch
# =============================================================================
set -uo pipefail

SERVER=192.168.0.212
ECHO=192.168.0.142
SUSER=ac
EKEY="$HOME/.ssh/id_hiyo_echo"
WATCH=0
FAIL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --server) SERVER="$2"; shift 2 ;;
    --echo) ECHO="$2"; shift 2 ;;
    --user) SUSER="$2"; shift 2 ;;
    --key) EKEY="$2"; shift 2 ;;
    --watch) WATCH=1; shift ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown: $1"; exit 1 ;;
  esac
done

pass() { printf '  \033[0;32m[OK]\033[0m   %-22s %s\n' "$1" "$2"; }
bad()  { printf '  \033[0;31m[FAIL]\033[0m %-22s %s\n' "$1" "$2"; FAIL=$((FAIL+1)); }
warn() { printf '  \033[0;33m[WARN]\033[0m %-22s %s\n' "$1" "$2"; }
hdr()  { printf '\n\033[1;36m%s\033[0m\n' "$*"; }

S="ssh -o ConnectTimeout=8 -o BatchMode=yes ${SUSER}@${SERVER}"
E="ssh -o ConnectTimeout=8 -o BatchMode=yes -i ${EKEY} root@${ECHO}"

check() { # label  remote-cmd  expected-substring
  local out
  out=$(eval "$2" 2>&1)
  if echo "$out" | grep -q "$3"; then pass "$1" "$(echo "$out" | head -1)"
  else bad "$1" "got: $(echo "$out" | head -1)"; fi
}

hdr "=== 1. 連線 ==="
$S "true" >/dev/null 2>&1 && pass "server ssh" "$SUSER@$SERVER" || bad "server ssh" "無法連線"
$E "true" >/dev/null 2>&1 && pass "echo ssh" "root@$ECHO" || bad "echo ssh" "無法連線（試 --via adb）"

hdr "=== 2. Echo 端 ==="
check "model"        "$E 'getprop ro.product.model'"            "AEOBC\|Echo"
check "android"      "$E 'getprop ro.build.version.release'"    "7\."
check "arch"         "$E 'uname -m'"                            "armv7l\|armv8l"
check "root"         "$E 'id'"                                  "uid=0"
for p in echo_probe echo_control echo_led_watchdog; do
  check "$p" "$E 'pidof $p'" "."
done
MIX=$($E 'getprop init.svc.mixer' 2>/dev/null | tr -d '\r')
if [ "$MIX" = running ] || [ "$MIX" = stopped ]; then pass "mixer" "$MIX"
else bad "mixer" "$MIX"; fi
check "led owner"    "$E 'cat /data/local/tmp/echo_led_owner'" "STOCK\|HIYO"

hdr "=== 3. Server 端 units ==="
for u in hermes-echo-tts hermes-echo-bridge hermes-echo-gadget hermes-echo-turn hermes-echo-stt hermes-echo-ha-media; do
  ST=$($S "systemctl --user is-active $u" 2>/dev/null | tr -d '\r')
  [ "$ST" = active ] && pass "$u" "active" || { [ "$ST" = inactive ] && warn "$u" "$ST（可能未啟用）" || bad "$u" "$ST"; }
done
for u in hermes-seed-vc hermes-gateway; do
  ST=$($S "systemctl --user is-active $u" 2>/dev/null | tr -d '\r')
  [ "$ST" = active ] && pass "$u" "active（外部依賴）" || warn "$u" "${ST:-不存在}（非本包）"
done

hdr "=== 4. Server ports ==="
LISTEN=$($S 'ss -tln' 2>/dev/null | tr -d '\r')
for p in 8792 8793 8795 8796 8797 8798; do
  if echo "$LISTEN" | grep -q ":$p "; then pass ":$p" "listening"
  else bad ":$p" "not listening"; fi
done
# Gadget gateway listener (LAN-facing by design)
if echo "$LISTEN" | grep -q ':8766 '; then pass ":8766 gadget" "Hermes Gadget listener"
else warn ":8766 gadget" "not listening（確認 Hermes Gadget plugin / gateway）"; fi

# Local shadow status must be online + paired.
GSTAT=$($S "python3 - <<'PY'
import json,socket
try:
 s=socket.create_connection(('127.0.0.1',8798),timeout=2); s.sendall(b'{\"op\":\"status\"}\n'); print(s.makefile('r').readline().strip())
except Exception as e: print(json.dumps({'ok':False,'error':str(e)}))
PY" 2>/dev/null | tail -1)
if echo "$GSTAT" | grep -q '"online": true' && echo "$GSTAT" | grep -q '"paired": true'; then
  pass "gadget shadow" "online + paired"
else
  warn "gadget shadow" "${GSTAT:-no response}"
fi

ST=$($S "systemctl --user is-active hermes-seed-vc" 2>/dev/null | tr -d '\r')
if echo "$LISTEN" | grep -q ":8790 "; then pass ":8790 seed-vc" "listening"
elif [ "$ST" = active ]; then warn ":8790 seed-vc" "active 但未 listen"
else warn ":8790 seed-vc" "seed-vc 未啟動（外部依賴）"; fi

hdr "=== 5. 音訊鏈路 ==="
if [ "$MIX" = stopped ]; then
  warn "link" "mixer 已停 → session 進行中，uplink 應在流動"
else
  warn "link" "mixer running → 目前 idle（無 session）"
fi
LOG=$($S "journalctl --user -u hermes-echo-bridge -n 40 --no-pager 2>/dev/null | grep 'UP frames'" 2>/dev/null | tail -1)
if [ -n "$LOG" ]; then
  echo "  最後統計: $LOG"
  echo "$LOG" | grep -q 'gaps=0 dupes=0' && pass "uplink health" "0 gaps / 0 dupes" \
    || warn "uplink health" "有 gap 或 dupe，檢查 WiFi"
  echo "$LOG" | grep -q 'eff_rate=16000.0Hz' && pass "sample rate" "16000.0 Hz 精準" \
    || warn "sample rate" "非精準 16000 Hz"
else
  warn "uplink" "log 中還沒有統計（服務剛啟動？）"
fi

hdr "=== 6. 磁碟 / 資源 ==="
DISK=$($S 'df -h / | tail -1' 2>/dev/null | tr -d '\r')
DISKPCT=$(echo "$DISK" | awk '{print $5}')
[ -n "$DISKPCT" ] && pass "server disk" "used $DISKPCT" || warn "server disk" "無法查詢"
LOAD=$($S 'uptime' 2>/dev/null | tr -d '\r')
[ -n "$LOAD" ] && pass "server load" "$LOAD" || warn "server load" "無法查詢"

echo
if [ "$FAIL" = 0 ]; then
  printf '\033[1;32m全部通過。\033[0m\n'
else
  printf '\033[1;31m%d 項失敗\033[0m — 看 docs/TROUBLESHOOTING.md\n' "$FAIL"
fi
[ "$WATCH" = 1 ] && { echo; echo "watch 模式（Ctrl-C 結束）…"; while true; do sleep 15; echo "--- $(date +%T) ---"; $S "journalctl --user -u hermes-echo-bridge -n 2 --no-pager | grep 'UP frames'"; done; }
exit $FAIL
