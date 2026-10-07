# 裝置端檔案清單（checksums 與實機一致）

從實機 `192.168.0.142` 逐 byte 複製，擷取時兩端 MD5 相同。
部署後可用下列指令驗證：

```sh
ssh root@<echo-ip> 'md5sum /data/local/tmp/echo_probe /data/local/tmp/echo_control /data/local/tmp/echo_led_watchdog'
```

| 檔案 | 目標路徑 | 大小 (bytes) | MD5 |
|---|---|---|---|
| `echo_probe` | `/data/local/tmp/echo_probe` | 55756 | `8d2b1f403345af2796a5f938b0f8e346` |
| `echo_control` | `/data/local/tmp/echo_control` | 20772 | `2785ac5c659af683d614e9d02c791261` |
| `echo_led_watchdog` | `/data/local/tmp/echo_led_watchdog` | 6980 | `f5539cbac48b5448f9f6610f099ef6bc` |
| `hiyo_supervisor.sh` | `/data/local/tmp/hiyo_supervisor.sh` | 8456 | `20fff6413ca7767429feb1828a63ae37` |
| `echo-led` | `/data/local/tmp/echo-led` | 4297 | `6bcfcfe8c636b914f81cca23db66d0f0` |
| `echo-cast` | `/data/local/tmp/echo-cast` | 4961 | `5a18a5f3fc1fa01ff5f1302ab8ea4700` |
| `hiyo_boot` | `/system/bin/hiyo_boot` | 876 | `9df0f29fb96c7f2413d0be592efba69c` |
| `hiyo.rc` | `/system/etc/init/hiyo.rc` | 902 | `1b3fc2ff81d7ad9c334b6a100d583cb8` |

---

## 二進位資訊

| 檔案 | 架構 | 說明 |
|---|---|---|
| `echo_probe` | ARMv7 EABI5, PIE | 音訊隧道：D24 擷取 + Speex AEC + D23 播放 + TCP :8792 |
| `echo_control` | ARMv7 EABI5, PIE | 控制：evdev 按鍵、mute、LED frame、音量、:8793 |
| `echo_led_watchdog` | ARMv7 EABI5, PIE | LED STOCK failover，1500 ms 週期 |
| `hiyo_boot` | ARMv7 EABI5, PIE | 876-byte exec stub |

全部由 **NDK r25c** 建立（`-pthread`）。裝置上沒有編譯器。

---

## 開機自啟：只能從 recovery 安裝

`hiyo.rc` 與 `hiyo_boot` 放在 `/system`，正常開機時該分割區是唯讀的
（system-as-root），無法用 remount 寫入。**必須用 TWRP ZIP 從 recovery 刷入。**

```sh
# 1. 產生 ZIP（update-binary 自我包含，不依賴 unzip）
./scripts/make-init-zip.sh

# 2. 推上裝置並刷入
adb push dist/hiyo-init-v1.zip /sdcard/
adb shell echo "--update_package=/sdcard/hiyo-init-v1.zip" > /cache/recovery/command
adb reboot recovery
```

安裝後驗證（實機目前的值）：

```sh
ssh root@<echo-ip> 'md5sum /system/etc/init/hiyo.rc /system/bin/hiyo_boot'
# 1b3fc2ff81d7ad9c334b6a100d583cb8  /system/etc/init/hiyo.rc
# 9df0f29fb96c7f2413d0be592efba69c  /system/bin/hiyo_boot
```

詳見 `docs/QUICKSTART.md` §5 與 `docs/PROVENANCE.md` §8。
