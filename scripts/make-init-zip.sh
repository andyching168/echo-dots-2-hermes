#!/usr/bin/env bash
# =============================================================================
# make-init-zip.sh — 產生 TWRP 可刷的 ZIP，把 Hiyo 開機自啟裝進 /system
#
# 為什麼需要 recovery:
#   本機是 system-as-root + A/B。正常開機時 /system 是 root image 的一部分，
#   掛載為唯讀 (/dev/root on / type ext4 (ro,...))，即使 root 也無法
#   mount -o rw,remount /system。唯一能寫入的方法是在 recovery 內把
#   /system_root 重新掛成 rw，而 TWRP 只接受用 ZIP 刷入的方式。
#
# 設計要點：
#   update-binary **自我包含** — payload 以 base64 內嵌在檔案尾端的
#   marker 區塊中，不依賴 unzip / busybox / mktemp，也不依賴 $3。
#
# 用法:
#   ./scripts/make-init-zip.sh              # 產生 dist/hiyo-init-v1.zip
#   ./scripts/make-init-zip.sh --out X.zip  # 指定輸出路徑
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$HERE/dist/hiyo-init-v1.zip"

while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    -h|--help) sed -n "2,20p" "$0" | sed "s/^# \{0,1\}//"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

RC="$HERE/device/init/hiyo.rc"
HB="$HERE/device/bin/hiyo_boot"
TPL="$HERE/device/init/update-binary.tpl"
for f in "$RC" "$HB" "$TPL"; do
  [ -f "$f" ] || { echo "缺少 $f" >&2; exit 1; }
done

command -v zip >/dev/null || { echo "需要 zip 指令 (dnf install zip / apt install zip)" >&2; exit 1; }

RC_SUM=$(md5sum "$RC" | cut -d" " -f1)
HB_SUM=$(md5sum "$HB" | cut -d" " -f1)

STAGE=$(mktemp -d)
trap "rm -rf $STAGE" EXIT
mkdir -p "$STAGE/META-INF/com/google/android"
UB="$STAGE/META-INF/com/google/android/update-binary"

# ---- 組裝 update-binary ---------------------------------------------------
# 1) 模板（安裝邏輯）
cat "$TPL" > "$UB"

# 2) payload marker 區塊：base64 一行一個，夾在 BEGIN/END 之間
{
  echo ""
  echo "# ---- embedded payload (base64) ----"
  echo "__HIYO_RC_BEGIN__"
  base64 -w0 "$RC"; echo
  echo "__HIYO_RC_END__"
  echo "__HIYO_HB_BEGIN__"
  base64 -w0 "$HB"; echo
  echo "__HIYO_HB_END__"
} >> "$UB"

chmod 755 "$UB"

# ---- updater-script: TWRP 只要求存在且非空 ----
cat > "$STAGE/META-INF/com/google/android/updater-script" <<\EOS
# Hiyo init autostart - logic lives in update-binary (self-contained)
EOS

mkdir -p "$(dirname "$OUT")"
rm -f "$OUT"
( cd "$STAGE" && zip -q -r -X "$OUT" . )

echo "產生: $OUT"
echo "  size:    $(du -h "$OUT" | cut -f1)"
echo "  payload: hiyo.rc ($RC_SUM)"
echo "           hiyo_boot ($HB_SUM)"
echo
echo "刷入方式 (二選一):"
echo "  A. ORS（免手動）:"
echo "     adb push $OUT /sdcard/hiyo-init-v1.zip"
echo "     adb shell echo \"--update_package=/sdcard/hiyo-init-v1.zip\" > /cache/recovery/command"
echo "     adb reboot recovery"
echo "  B. 手動: adb reboot recovery → Install → 選 hiyo-init-v1.zip"