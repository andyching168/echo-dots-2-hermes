#!/system/bin/sh
# =============================================================================
# hiyo_supervisor.sh  --  Hiyo cold-boot device supervisor
#
# Deployed at:   /data/local/tmp/hiyo_supervisor.sh   (chmod 755)
# Launched by:   /system/bin/hiyo_boot  (init `service hiyo`, seclabel u:r:su:s0)
# Runs as:       root, SELinux domain u:r:su:s0, Android toybox/mksh
#
# Owns the three device-side Hiyo processes:
#   echo_control  <host> 8793 grab exclusive   (LED owner / buttons / mute)
#   echo_led_watchdog 1500                     (independent STOCK failover)
#   echo_probe    net <host> 8792 0            (AEC uplink / downlink)
#
# Invariants (do NOT change):
#   * never permanently stop mixer / ledcontroller / Puffin
#   * on every child exit and on TERM/INT: kill children, then restore the
#     stock mixer + stock ledcontroller and force LED ownership to STOCK
#   * physical mute is never software-unmuted (echo_control owns that)
#   * bounded restart backoff 2s -> 5s -> 15s -> 60s cap
#   * no persistent props, no /system writes, no init edits
# =============================================================================

HOST=192.168.0.212
PORT=8792
CTL_PORT=8793
TMP=/data/local/tmp
LOG=$TMP/hiyo_supervisor.log

# Standard Android dirs so tool lookups never depend on boot order.
# NOTE: this (D2A) environment has no Magisk/busybox; only toybox applets.
# toybox is available as /system/bin/toybox; `nc` has no standalone symlink.
PATH=/sbin:/system/sbin:/system/bin:/system/xbin
export PATH
# init does not set these for services; bionic's tzdata lookup wants them.
ANDROID_ROOT=/system
ANDROID_DATA=/data
export ANDROID_ROOT ANDROID_DATA
NC="/system/bin/toybox nc"

log() { echo "[$(date '+%F %T')] $*"; }
exec >>"$LOG" 2>&1

# -----------------------------------------------------------------------------
# process helpers
# -----------------------------------------------------------------------------
pids_of() { pidof "$1" 2>/dev/null; }

is_running() { [ -n "$(pids_of "$1")" ]; }

stop_children() {
    for p in echo_probe echo_control echo_led_watchdog; do
        if is_running "$p"; then
            log "stopping $p ($(pids_of "$p"))"
            kill $(pids_of "$p") 2>/dev/null
        fi
    done
    sleep 2
    for p in echo_probe echo_control echo_led_watchdog; do
        if is_running "$p"; then
            log "force-killing $p ($(pids_of "$p"))"
            kill -9 $(pids_of "$p") 2>/dev/null
        fi
    done
    sleep 1
}

# Restore the proven stock baseline. Never destroys anything: only (re)starts
# init-managed services and resets the logical LED owner to STOCK.
recover_stock() {
    if [ "$(getprop init.svc.mixer)" != "running" ]; then
        log "recover: restarting stock mixer"
        /system/bin/start mixer 2>/dev/null
    fi
    if [ "$(getprop init.svc.ledcontroller)" != "running" ]; then
        log "recover: restarting stock ledcontroller"
        /system/bin/start ledcontroller 2>/dev/null
    fi
    [ -f "$TMP/echo_led_owner" ] && printf 'STOCK\n' >"$TMP/echo_led_owner" 2>/dev/null
    log "recover: mixer=$(getprop init.svc.mixer) audio_mixer.init=$(getprop audio_mixer.init) led=$(getprop init.svc.ledcontroller) PuffinApp=$(pids_of PuffinApp)"
}

# -----------------------------------------------------------------------------
# readiness gates
# -----------------------------------------------------------------------------
wait_network() {
    local n=0
    while :; do
        # wlan0 DHCP lease (works even when `ip` is unavailable on this build)
        if [ -n "$(getprop dhcp.wlan0.ipaddress)" ]; then
            log "network ready: wlan0=$(getprop dhcp.wlan0.ipaddress) gw=$(getprop dhcp.wlan0.gateway)"
            return 0
        fi
        n=$((n+1)); [ $((n % 12)) -eq 0 ] && log "waiting for WiFi ($n)"
        sleep 5
    done
}

wait_server() {
    local n=0
    while :; do
        # toybox nc: connect succeeds => bridge listening on :8792
        if $NC -w 2 "$HOST" "$PORT" </dev/null >/dev/null 2>&1; then
            log "Ubuntu bridge $HOST:$PORT reachable"
            return 0
        fi
        n=$((n+1)); [ $((n % 12)) -eq 0 ] && log "waiting for $HOST:$PORT ($n)"
        sleep 5
    done
}

wait_mixer() {
    local n=0
    while [ "$(getprop init.svc.mixer)" != "running" ] || [ "$(getprop audio_mixer.init)" != "true" ]; do
        n=$((n+1)); [ $((n % 12)) -eq 0 ] && log "waiting for stock mixer baseline ($n): svc=$(getprop init.svc.mixer) init=$(getprop audio_mixer.init)"
        sleep 5
    done
    log "stock mixer baseline healthy"
}

# -----------------------------------------------------------------------------
# child launchers (idempotent)
# -----------------------------------------------------------------------------
start_control() {
    is_running echo_control && return 0
    log "starting echo_control $HOST $CTL_PORT grab exclusive"
    setsid "$TMP/echo_control" "$HOST" "$CTL_PORT" grab exclusive \
        >"$TMP/echo_control.boot.log" 2>&1 </dev/null &
    sleep 2
}

ensure_watchdog() {
    is_running echo_led_watchdog && return 0
    log "starting echo_led_watchdog 1500"
    setsid "$TMP/echo_led_watchdog" 1500 \
        >"$TMP/echo_led_watchdog.log" 2>&1 </dev/null &
    sleep 1
}

start_probe() {
    is_running echo_probe && return 0
    log "starting echo_probe net $HOST $PORT 0"
    setsid "$TMP/echo_probe" net "$HOST" "$PORT" 0 \
        >"$TMP/echo_probe.boot.log" 2>&1 </dev/null &
    sleep 2
}

# -----------------------------------------------------------------------------
# shutdown / signal handling
# -----------------------------------------------------------------------------
cleanup() {
    log "signal received -> stopping children, restoring stock"
    stop_children
    recover_stock
    exit 0
}
trap cleanup TERM INT HUP

# -----------------------------------------------------------------------------
# SSH (dropbear) — remote root shell without USB
# -----------------------------------------------------------------------------
SSH_DIR=/data/hiyo/ssh
ensure_ssh() {
    # FireOS INPUT policy is DROP; allow the SSH port on wlan0 (idempotent).
    if [ -x /system/bin/iptables ]; then
        /system/bin/iptables -C INPUT -i wlan0 -p tcp --dport 22 -j ACCEPT 2>/dev/null || \
            /system/bin/iptables -I INPUT 1 -i wlan0 -p tcp --dport 22 -j ACCEPT 2>/dev/null
    fi
    if ! is_running dropbear && [ -x "$SSH_DIR/dropbear" ]; then
        log "starting dropbear sshd on :22"
        setsid "$SSH_DIR/dropbear" -r "$SSH_DIR/host_ed25519" -r "$SSH_DIR/host_rsa" \
            -p 22 -P "$SSH_DIR/dropbear.pid" \
            >>"$SSH_DIR/dropbear.log" 2>&1 </dev/null &
        sleep 1
    fi
}

# -----------------------------------------------------------------------------
# main supervise loop
# -----------------------------------------------------------------------------
log "hiyo_supervisor start (host=$HOST port=$PORT)"

backoff=2
mdns_kicked=0
while :; do
    wait_network
    if [ "$mdns_kicked" = "0" ]; then
        # avahi-daemon is started by init before DHCP completes and then only
        # advertises its IPv6 address; set a friendly hostname and restart it
        # once so the IPv4 A record (<host>.local) is published.
        echo echo-dot2 > /proc/sys/kernel/hostname 2>/dev/null
        stop avahi-daemon 2>/dev/null
        sleep 1
        start avahi-daemon 2>/dev/null
        mdns_kicked=1
        log "mdns: host=$(cat /proc/sys/kernel/hostname 2>/dev/null) avahi restarted after network"
    fi
    ensure_ssh
    wait_server
    wait_mixer

    start_control
    ensure_watchdog
    start_probe
    log "stack up: ctl=$(pids_of echo_control) wd=$(pids_of echo_led_watchdog) probe=$(pids_of echo_probe)"

    # Watch until one of the two long-running children dies.
    healthy=0
    while is_running echo_control && is_running echo_probe; do
        sleep 3
        healthy=$((healthy+1))
        [ $((healthy % 20)) -eq 0 ] && ensure_ssh   # re-assert firewall/sshd every ~60s
        [ $healthy -ge 100 ] && backoff=2   # 5 min stable -> reset backoff
    done

    log "child exit detected: ctl=$(pids_of echo_control) probe=$(pids_of echo_probe) wd=$(pids_of echo_led_watchdog); recovering"
    stop_children
    recover_stock

    log "restart in ${backoff}s"
    sleep "$backoff"
    case "$backoff" in
        2)  backoff=5 ;;
        5)  backoff=15 ;;
        15) backoff=60 ;;
        *)  backoff=60 ;;
    esac
done
