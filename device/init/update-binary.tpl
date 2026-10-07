#!/sbin/sh
#
# Hiyo init autostart installer (TWRP / recovery)
#
# 自我包含：payload 以 base64 內嵌在檔案尾端，不需要 unzip / busybox。
# recovery 環境的工具不保證存在，所以能少依賴就少依賴。
#
# 使用方式（TWRP 會自動呼叫）:
#   update-binary <api> <outfd> <zipfile>
#
OUTFD=$2

ui_print() { echo -e "ui_print $1\nui_print" >> /proc/self/fd/$OUTFD; }
fail() { ui_print "Hiyo: ERROR - $1"; exit 1; }

# ---- 找 system 掛載點 ----
SYSTEM=""
if [ -d /system_root/system ]; then
    SYSTEM=/system_root
    ui_print "Hiyo: system-as-root at /system_root"
elif [ -d /system/etc ]; then
    SYSTEM=/system
    ui_print "Hiyo: legacy system at /system"
else
    fail "no system partition found"
fi

ui_print "Hiyo: remounting $SYSTEM rw"
mount -o rw,remount "$SYSTEM" 2>/dev/null
mount -o rw,remount / 2>/dev/null

touch "$SYSTEM/.hiyo_rw" 2>/dev/null || fail "cannot write $SYSTEM (still read-only)"
rm -f "$SYSTEM/.hiyo_rw"

# ---- 找 base64 解碼工具 ----
B64=""
for c in toybox busybox base64; do
    if command -v "$c" >/dev/null 2>&1; then
        if [ "$c" = base64 ]; then
            B64="$c"; break
        elif $c base64 </dev/null >/dev/null 2>&1; then
            B64="$c base64"; break
        fi
    fi
done
[ -n "$B64" ] || fail "no base64 tool available"
ui_print "Hiyo: decoder = $B64"

WORK=/tmp/hiyo_payload
rm -rf "$WORK"
mkdir -p "$WORK" || fail "cannot create $WORK"

# 從本檔自身抽出 payload（夾在 marker 之間）
extract() {
    # $1 = tag (e.g. HIYO_RC)
    sed -n "/^__${1}_BEGIN__$/,/^__${1}_END__$/p" "$0" | sed '1d;$d'
}

ui_print "Hiyo: extracting payload"
extract HIYO_RC | $B64 -d > "$WORK/hiyo.rc"     || fail "decode hiyo.rc failed"
extract HIYO_HB | $B64 -d > "$WORK/hiyo_boot"   || fail "decode hiyo_boot failed"

[ -s "$WORK/hiyo.rc" ]   || fail "hiyo.rc is empty"
[ -s "$WORK/hiyo_boot" ] || fail "hiyo_boot is empty"

# ---- 安裝 ----
ui_print "Hiyo: installing init hook"
mkdir -p "$SYSTEM/system/etc/init" "$SYSTEM/system/bin"

cp -f "$WORK/hiyo.rc"   "$SYSTEM/system/etc/init/hiyo.rc" || fail "cannot write hiyo.rc"
cp -f "$WORK/hiyo_boot" "$SYSTEM/system/bin/hiyo_boot"    || fail "cannot write hiyo_boot"
chmod 644 "$SYSTEM/system/etc/init/hiyo.rc"
chmod 755 "$SYSTEM/system/bin/hiyo_boot"

# SELinux context：init 要正確的 label 才會載入 rc。找不到工具不致命。
for RC in /system/bin/restorecon /sbin/restorecon /system_root/system/bin/restorecon; do
    if [ -x "$RC" ]; then
        "$RC" "$SYSTEM/system/etc/init/hiyo.rc" 2>/dev/null
        "$RC" "$SYSTEM/system/bin/hiyo_boot" 2>/dev/null
        ui_print "Hiyo: restorecon done"
        break
    fi
done

# ---- 驗證 ----
if command -v md5sum >/dev/null 2>&1; then
    GOT=$(md5sum "$SYSTEM/system/etc/init/hiyo.rc" | cut -d' ' -f1)
    ui_print "Hiyo: installed rc md5 = $GOT"
fi

sync
rm -rf "$WORK"
umount "$SYSTEM" 2>/dev/null

ui_print "Hiyo: installed OK - reboot to activate"
exit 0
