#!/usr/bin/env bash
# =============================================================================
# deploy-server.sh — 部署 Hiyo Echo 的 Ubuntu 端（大腦）
#
# 把本包的 server/ + units/ 推到目標主機，建 venv，裝 systemd user unit，
# 開啟 linger，然後驗證每個 port 都有 listen。
#
# 用法:
#   ./scripts/deploy-server.sh --host 192.168.0.212 --user ac
#   ./scripts/deploy-server.sh --host localhost          # 本機部署
#
# 選項:
#   --host H        目標主機 (預設 192.168.0.212)
#   --user U        SSH 使用者 (預設 ac)
#   --dir D         遠端安裝目錄 (預設 ~/voice/echo-bridge)
#   --no-venv       跳過 venv 建立 / pip install
#   --no-services   只複製檔案，不裝 systemd unit
#   --no-wakeword   不裝 openwakeword（節省時間；喚醒詞會失效）
#   --dry-run       只印出要做的事
# =============================================================================
set -euo pipefail

HOST=192.168.0.212
USER_NAME=ac
DEST=""
NO_VENV=0
NO_SERVICES=0
NO_WAKEWORD=0
DRY_RUN=0
GADGET_PLUGIN_URL="https://github.com/Adolanium/hermes-gadget-sdk.git#plugin"
GADGET_PLUGIN_REF="d661b8f2787bc79dd48a7c399ae76d2bbc0b7d41"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --user) USER_NAME="$2"; shift 2 ;;
    --dir)  DEST="$2"; shift 2 ;;
    --no-venv) NO_VENV=1; shift ;;
    --no-services) NO_SERVICES=1; shift ;;
    --no-wakeword) NO_WAKEWORD=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown option: $1" >&2; usage 1 ;;
  esac
done

[ -n "$DEST" ] || DEST="~/voice/echo-bridge"

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[0;32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[0;33m!\033[0m %s\n' "$*"; }
die()  { printf '    \033[0;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
run()  { if [ "$DRY_RUN" = 1 ]; then echo "    [dry-run] $*"; else "$@"; fi; }

# ── remote execution helper ──────────────────────────────────────────────────
if [ "$HOST" = "localhost" ] || [ "$HOST" = "127.0.0.1" ] || [ "$HOST" = "$(hostname)" ]; then
  LOCAL=1
else
  LOCAL=0
fi

rsh() {
  if [ "$LOCAL" = 1 ]; then bash -c "$*"
  else ssh -o ConnectTimeout=10 -o BatchMode=yes "${USER_NAME}@${HOST}" "$*"; fi
}
rcp() {
  # $1 = local path, $2 = remote path
  if [ "$LOCAL" = 1 ]; then cp -a "$1" "$2"
  else
    tar cf - -C "$(dirname "$1")" "$(basename "$1")" \
      | ssh -o BatchMode=yes "${USER_NAME}@${HOST}" "tar xf - -C '$2'"
  fi
}

step "目標: ${USER_NAME}@${HOST}  →  $DEST"
[ -d "$HERE/server" ] || die "找不到 $HERE/server — 請從本包根目錄執行"

# ── 1. 連線與前置檢查 ────────────────────────────────────────────────────────
step "1/6 連線與前置檢查"
if [ "$LOCAL" = 0 ]; then
  rsh "true" >/dev/null 2>&1 || die "SSH 連不上 ${USER_NAME}@${HOST}（檢查金鑰 / ssh config）"
  ok "SSH OK"
fi
PYV=$(rsh "python3 -V 2>&1 | sed 's/Python //'" | awk '{print $1}' || echo none)
[ "$PYV" != none ] || die "遠端沒有 python3"
ok "python3 $PYV"
case "$PYV" in 3.9|3.1[0-9]) ok "版本足夠";; *) warn "Python $PYV 未經測試（建議 3.11+）";; esac

# ── 2. 建立目錄並複製 server/ ────────────────────────────────────────────────
step "2/6 複製 server/  →  $DEST"
rsh "mkdir -p $DEST $DEST/captures $DEST/logs $DEST/test_audio"
if [ "$DRY_RUN" = 1 ]; then
  echo "    [dry-run] 複製 $HERE/server/ → $DEST/"
else
  if [ "$LOCAL" = 1 ]; then
    DEST_EXP="${DEST/#\~/$HOME}"
    rsync -a --exclude='__pycache__' "$HERE/server/" "$DEST_EXP/" 2>/dev/null \
      || { mkdir -p "$DEST_EXP"; cp -a "$HERE/server/." "$DEST_EXP/"; }
  else
    ( cd "$HERE/server" && tar cf - --exclude='__pycache__' . ) \
      | ssh -o BatchMode=yes "${USER_NAME}@${HOST}" "tar xf - -C $DEST"
  fi
fi
ok "server 檔案就位"

# ── 3. venv ──────────────────────────────────────────────────────────────────
if [ "$NO_VENV" = 1 ]; then
  step "3/6 跳過 venv（--no-venv）"
else
  step "3/6 建立 venv 並安裝依賴"
  rsh "test -x $DEST/.venv/bin/python" 2>/dev/null && ok "venv 已存在" || {
    run rsh "python3 -m venv $DEST/.venv" && ok "venv 建立完成"
  }
  REQ="$DEST/requirements.txt"
  if [ "$NO_WAKEWORD" = 1 ]; then
    rsh "grep -v -iE 'openwakeword|onnxruntime|scipy' $REQ > $DEST/requirements.noww.txt"
    REQ="$DEST/requirements.noww.txt"
    warn "略過喚醒詞依賴（喚醒詞將無法使用）"
  fi
  run rsh "$DEST/.venv/bin/pip install -q --upgrade pip"
  run rsh "$DEST/.venv/bin/pip install -q -r $REQ" || die "pip install 失敗"
  ok "依賴安裝完成"
fi

# ── 3.5 Hermes platform plugins ─────────────────────────────────────────────
step "3.5/6 安裝 Hermes Gadget + legacy Echo plugin"
HERMES_BIN=$(rsh 'if command -v hermes >/dev/null 2>&1; then command -v hermes; elif [ -x "$HOME/.local/bin/hermes" ]; then printf %s "$HOME/.local/bin/hermes"; fi' | tr -d '\r')
if [ -z "$HERMES_BIN" ]; then
  warn "找不到 Hermes CLI；先安裝 Hermes，之後再安裝 Gadget plugin"
else
  if [ "$DRY_RUN" = 1 ]; then
    echo "    [dry-run] install Gadget platform plugin: $GADGET_PLUGIN_URL --ref $GADGET_PLUGIN_REF"
    echo "    [dry-run] copy hermes-plugins/hermes-echo -> ~/.hermes/plugins/hermes-echo"
  else
    REMOTE_HOME=$(rsh 'printf %s "$HOME"' | tr -d '\r')
    rsh "mkdir -p '$REMOTE_HOME/.hermes/plugins/hermes-echo'"
    if [ "$LOCAL" = 1 ]; then
      cp -a "$HERE/hermes-plugins/hermes-echo/." "$REMOTE_HOME/.hermes/plugins/hermes-echo/"
    else
      ( cd "$HERE/hermes-plugins/hermes-echo" && tar cf - . ) \
        | ssh -o BatchMode=yes "${USER_NAME}@${HOST}" "tar xf - -C '$REMOTE_HOME/.hermes/plugins/hermes-echo'"
    fi
    if rsh "$HERMES_BIN plugins list --plain 2>/dev/null | grep -Eq '[[:space:]]gadget$'"; then
      ok "Hermes Gadget plugin 已存在（保留現有安裝）"
    else
      rsh "$HERMES_BIN plugins install '$GADGET_PLUGIN_URL' --ref '$GADGET_PLUGIN_REF' --enable" >/dev/null \
        || die "Hermes Gadget plugin 安裝失敗"
    fi
    rsh "$HERMES_BIN plugins enable hermes-echo" >/dev/null 2>&1 || true
    rsh "$HERMES_BIN config set platforms.gadget.enabled true" >/dev/null
    rsh "$HERMES_BIN config set platforms.gadget.extra.host 0.0.0.0" >/dev/null
    rsh "$HERMES_BIN config set platforms.gadget.extra.port 8766" >/dev/null
    ok "Gadget platform enabled on 0.0.0.0:8766；hermes-echo 保留為 legacy-only"
  fi
fi

# ── 4. systemd user units ────────────────────────────────────────────────────
if [ "$NO_SERVICES" = 1 ]; then
  step "4/6 跳過 systemd（--no-services）"
else
  step "4/6 安裝 systemd user units"
  rsh "mkdir -p ~/.config/systemd/user"
  if [ "$DRY_RUN" = 1 ]; then
    echo "    [dry-run] 複製 units/*.service + *.d/ 並改寫路徑 → ~/.config/systemd/user/"
  else
    # 單元檔內的路徑是實機擷取值 (/home/ac, /home/ac/voice/echo-bridge)。
    # 換到別的機器時改寫成目標的 HOME 與安裝目錄。
    REMOTE_HOME=$(rsh 'printf %s "$HOME"' | tr -d '\r')
    DEST_ABS=$(rsh "cd $DEST && pwd" | tr -d '\r')
    UTMP=$(mktemp -d)
    cp -a "$HERE/units/." "$UTMP/"
    rm -rf "$UTMP/examples"
    grep -rl '/home/ac/voice/echo-bridge' "$UTMP" 2>/dev/null \
      | xargs -r sed -i "s|/home/ac/voice/echo-bridge|$DEST_ABS|g"
    grep -rl '/home/ac' "$UTMP" 2>/dev/null \
      | xargs -r sed -i "s|/home/ac|$REMOTE_HOME|g"
    # ExecStart 內的 --host 是對 Echo 綁定的 LAN IP；若目標主機不同則改寫
    SRV_IP=$(rsh "hostname -I 2>/dev/null | awk '{print \$1}'" | tr -d '\r')
    if [ -n "$SRV_IP" ] && [ "$SRV_IP" != "192.168.0.212" ]; then
      grep -rl -- '--host 192.168.0.212' "$UTMP" 2>/dev/null \
        | xargs -r sed -i "s|--host 192.168.0.212|--host $SRV_IP|g"
      warn "ExecStart --host 改寫為 $SRV_IP"
    fi
    if [ "$LOCAL" = 1 ]; then
      cp -a "$UTMP/." "$HOME/.config/systemd/user/"
    else
      ( cd "$UTMP" && tar cf - . ) \
        | ssh -o BatchMode=yes "${USER_NAME}@${HOST}" "tar xf - -C ~/.config/systemd/user"
    fi
    rm -rf "$UTMP"
    ok "units 已安裝 (路徑改寫: $REMOTE_HOME / $DEST_ABS)"
  fi
  run rsh "systemctl --user daemon-reload"
  ok "daemon-reload"
fi

# ── 5. linger + 啟動 ─────────────────────────────────────────────────────────
step "5/6 開啟 linger 並啟動服務"
LINGER=$(rsh "loginctl show-user \$(whoami) 2>/dev/null | grep -o 'Linger=yes'" || true)
if [ -n "$LINGER" ]; then
  ok "linger 已開啟"
else
  warn "linger 未開啟 — 需要 sudo 才能設定開機自啟"
  echo "      請在目標主機執行:  sudo loginctl enable-linger ${USER_NAME}"
  if [ "$DRY_RUN" = 0 ] && [ "$LOCAL" = 0 ]; then
    read -r -p "    現在用 sudo 開啟? [y/N] " a
    [ "$a" = y ] && ssh -t "${USER_NAME}@${HOST}" "sudo loginctl enable-linger ${USER_NAME}"
  fi
fi

# 只啟動本包負責的 unit；Hermes 本体 (seed-vc/gateway) 若不在則 warn 不 fail
UNITS="hermes-echo-tts hermes-echo-bridge hermes-echo-gadget hermes-echo-turn hermes-echo-stt hermes-echo-ha-media"
for u in $UNITS; do
  if rsh "systemctl --user cat $u.service >/dev/null 2>&1"; then
    run rsh "systemctl --user enable --now $u" && ok "enabled+started $u"
  else
    warn "找不到 $u.service（跳過）"
  fi
done
# 依賴檢查
for dep in hermes-gateway; do
  if rsh "systemctl --user cat $dep.service >/dev/null 2>&1"; then
    rsh "systemctl --user is-active $dep" | grep -q '^active' \
      && ok "$dep active" || warn "$dep 未 active — bridge 可能等不到依賴"
  else
    warn "沒有 $dep（Hermes Gateway 是 Question/Gadget 模式必要依賴）"
  fi
done

# ── 6. 驗證 ──────────────────────────────────────────────────────────────────
step "6/6 驗證"
sleep 3
for p in 8792 8793 8795 8796 8797 8798; do
  if rsh "ss -tln 2>/dev/null | grep -q ':$p '"; then ok "port $p listening"
  else warn "port $p 尚未 listen（看 journalctl --user -u hermes-echo-bridge）"; fi
done

cat <<EOF

\033[1;32m部署完成。\033[0m

  狀態:   $ rsh "systemctl --user status hermes-echo-bridge"
  日誌:   $ rsh "journalctl --user -u hermes-echo-bridge -f"
  驗證:   $HERE/scripts/verify.sh --server ${HOST}

下一步: 部署 Echo 端
  $HERE/scripts/deploy-device.sh --host <echo-ip> --server ${HOST}
EOF
