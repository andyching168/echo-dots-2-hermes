# RECOVERY_GUIDE — 從未解鎖到 Hiyo 裝好

> **權威來源**：XDA 的 *[UNLOCK][ROOT][TWRP][UNBRICK] Amazon Echo Dot 2nd Gen / 2016 (biscuit)*
> 討論串（作者 @R0rt1z2，工具 by @k4y0z）。
> 上游來源與引用說明見 [`reference/XDA_ROOT_REFERENCE.md`](reference/XDA_ROOT_REFERENCE.md)；本專案不重製論壇全文。
>
> 本文件只負責**標出解鎖流程與 Hiyo 部署之間的接點**，以及補上原文沒寫、
> 但 Hiyo 會用到的環境細節。**解鎖本身請以原文為準。**

---

## 0. 全貌

XDA 原文涵蓋「解鎖 → 刷 TWRP → 刷 FireOS → 取得 root」。
**做完原文，裝置就是一台 rooted Echo；Hiyo 從那裡接手。**

```
  ═══ XDA 原文的範圍 ═══                        ═══ Hiyo 的範圍 ═══

  fastbrick.sh  (Option 1)                      make-init-zip.sh
    或 bootrom-step.sh (Option 2)                 (產生 init ZIP)
         │                                            │
         ▼                                            ▼
  fastboot-step.sh  → 刷 TWRP 進 recovery        adb push + ORS
         │                                            │
         ▼                                            ▼
  刷 FireOS 6 (NS6574/7623) × 2 次              hiyo.rc / hiyo_boot
         │                                      寫進 /system
         ▼                                            │
  刷 boot-root ZIP → root 可用                       ▼
         │                                     開機自動啟動 Hiyo
         └──────────────► 接點 ◄──────────────────────┘
```

| 階段 | 誰負責 | 做什麼 |
|---|---|---|
| **1. 解鎖 + 刷 TWRP** | **XDA 原文** | `fastbrick.sh`／`bootrom-step.sh` 解鎖，`fastboot-step.sh` 刷 TWRP |
| **2. 刷 FireOS + root** | **XDA 原文** | 刷 FireOS 6 兩次（A/B），刷 `boot-root` ZIP |
| **3. 確認可用** | 本文件 §2 | 正常開機，確認 `adb root` |
| **4. 產生 Hiyo ZIP** | 本文件 §3 | `make-init-zip.sh` |
| **5. 進 TWRP 刷入** | 本文件 §4 | `adb push` + ORS |

> ⚠️ 階段 1、2 有**永久變磚**的風險，請完整讀過原文再動手。
> 本包不重複那些步驟，也不為解鎖流程負責。

---

## 1. 前置：你需要先拿到什麼

| 項目 | 來源 | 本包位置 |
|---|---|---|
| amonet 套件 | XDA 原文附件 | `refs/amonet-biscuit-v2.0.0/` ✓ |
| FireOS 6 韌體（NS6574/7623）| FTVDB | `refs/artifacts/update-kindle-biscuit_puffin-NS6574_user_7623_0013121734532.bin` ✓ |
| `boot-root` ZIP | 原文附件 | `refs/artifacts/boot-root.zip` ✓ |

**三個檔案本包都有**，且實機版本已核對一致（見 §5）。

---

## 2. 階段 3：確認 root 可用（Hiyo 的起點）

原文做完（刷完 `boot-root` 並 `adb reboot`）之後，裝置會**預設就開 root shell**。
先確認這件事：

```bash
adb devices
adb shell id
# 期望：
# uid=0(root) gid=0(root) groups=0(root),1004(input),1007(log),
#   1011(adb),1015(sdcard_rw),1028(sdcard_r),3001(net_bt_admin),
#   3002(net_bt),3003(inet),3006(net_bw_stats),3009(readproc)
#   context=u:r:su:s0

adb shell getprop ro.boot.unlocked_kernel
# true   ← 解鎖成功的證據
```

### 檢查清單

- [ ] 系統正常開機，LED 正常（閒置白色）
- [ ] `adb shell id` 顯示 `uid=0`
- [ ] `getprop ro.boot.unlocked_kernel` = `true`
- [ ] `adb shell getprop ro.build.version.name` = `Fire OS 6.x (NS6574/...)`
- [ ] WiFi 連上，能 `ping` 到你的 server

**這些都過了才進階段 4。** 沒過就先解決 —— 不要在只有 headless TWRP 能連的
狀態下瞎刷。

### 2.1 順便把網路 adb 開起來（可選）

```bash
adb shell setprop service.adb.tcp.port 5555
adb shell stop adbd && adb shell start adbd
adb connect <echo-ip>:5555
```

> ⚠️ **runtime prop，重開機就沒了。** 要持久化需要 `persist.adb.tcp.port`，
> 那屬於改動系統設定，本包不代做。
> 而且 recovery 內的 adb 一定得走 USB，所以這只省下平時插拔。

---

## 3. 階段 4：產生 Hiyo 的 ZIP（電腦上）

```bash
cd echo-dots-2-hermes
./scripts/make-init-zip.sh
# → dist/hiyo-init-v1.zip
```

自我包含的 ZIP：payload 以 base64 內嵌在 `update-binary`，不依賴 recovery 內的
`unzip`。

---

## 4. 階段 5：進 TWRP 刷入

### 4.1 推進裝置（趁系統還開著）

⚠️ **recovery 裡沒有 WiFi。** ZIP 必須在重開機**之前**推進去。

```bash
adb push dist/hiyo-init-v1.zip /sdcard/hiyo-init-v1.zip

# 驗證推送完整（推壞了進 TWRP 就白跑）
adb shell md5sum /sdcard/hiyo-init-v1.zip
md5sum dist/hiyo-init-v1.zip
# 兩個數字必須一樣
```

### 4.2 進 TWRP

原文列了三種方式，第一種最方便：

```bash
adb reboot recovery
```

其他兩種（原文）：
- 開機時**按住 Volume Up**（exploit 重新對應過按鍵）→ 靜態白燈
- 用 amonet 的 `boot-recovery.sh`

### 4.3 刷入

⚠️ **本機是 headless TWRP**（原文也說了「In order to use TWRP, you have to use
the TWRP command line」）—— 沒有圖形介面，要用命令列。

因此有兩種做法：

**方法 A：ORS（本包推薦，免手動）**

```bash
adb shell echo "--update_package=/sdcard/hiyo-init-v1.zip" > /cache/recovery/command
adb reboot recovery
```

**方法 B：TWRP 命令列（原文的 `twrp install`）**

```bash
adb shell twrp install /sdcard/hiyo-init-v1.zip
```

本包的 ZIP 兩種都支援 —— 它是標準的 TWRP flashable ZIP，
`update-binary` 走的是同一套介面。

### 4.4 看 LED 判斷成功失敗（原文的機制）

| LED | 意義 |
|---|---|
| 白色（靜止）| 閒置 |
| 環狀跑動 | 安裝中，當進度條 |
| **綠色** | **成功** |
| **紅色（幾秒）** | **失敗** |

所以不用猜 —— 刷完看燈就知道。

### 4.5 文字輸出（若用 adb 看得到的話）

```
Hiyo: system-as-root at /system_root
Hiyo: remounting /system_root rw
Hiyo: decoder = toybox base64
Hiyo: extracting payload
Hiyo: installing init hook
Hiyo: restorecon done
Hiyo: installed rc md5 = 1b3fc2ff81d7ad9c334b6a100d583cb8
Hiyo: installed OK - reboot to activate
```

### 4.6 回系統

```bash
adb reboot
```

---

## 5. 驗證

```bash
# 檔案與權限
ssh root@<echo-ip> 'ls -l /system/etc/init/hiyo.rc /system/bin/hiyo_boot'
# -rw-r--r-- ... 902  /system/etc/init/hiyo.rc
# -rwxr-xr-x ... 876  /system/bin/hiyo_boot

# 內容
ssh root@<echo-ip> 'md5sum /system/etc/init/hiyo.rc /system/bin/hiyo_boot'
# 1b3fc2ff81d7ad9c334b6a100d583cb8  /system/etc/init/hiyo.rc
# 9df0f29fb96c7f2413d0be592efba69c  /system/bin/hiyo_boot
```

### 最關鍵：測開機自啟

```bash
ssh root@<echo-ip> reboot
sleep 90
ssh root@<echo-ip> 'for p in echo_probe echo_control echo_led_watchdog; do echo "$p: $(pidof $p)"; done'
ssh root@<echo-ip> 'tail -20 /data/local/tmp/hiyo_supervisor.log'
```

三個程序都有 pid → **這台 Echo 現在插電就會自己變成 Hiyo。**

---

## 6. 出錯了怎麼辦

### 6.1 解鎖／刷機層（階段 1–2）

這些屬於 XDA 原文範疇，**請查原文**。常見的幾種：

| 症狀 | 說明 |
|---|---|
| `Device mismatch` | payload 不對，確認是 biscuit（2016 2nd gen）|
| `eMMC-RO` / permanent read-only | eMMC 壽命到了，硬體故障，無解 |
| 刷完 FireOS 開不了機 | 只刷了一次。**A/B 要刷兩次** |
| 開了但沒有 root | 忘了刷 `boot-root` ZIP |
| 進不去 bootrom | 測試點沒短路好，或 ModemManager 沒關 |

### 6.2 Hiyo 層（階段 5）

| 症狀 | 原因 | 解法 |
|---|---|---|
| `Zip file is corrupt` | 傳輸中斷 | 重新 push 並比對 md5 |
| 找不到 `/sdcard/hiyo-init-v1.zip` | 路徑錯 | recovery 內 `/sdcard` 是 `/data/media` 的 bind；改放 `/data/media/` |
| `Hiyo: ERROR - no system partition found` | 不是 TWRP，或 slot 掛錯 | `adb shell "ls -d /system_root/system /system/etc"` |
| `cannot write /system_root` | remount 失敗 | 確認是 TWRP 而非原廠 recovery |
| 刷成功但開機沒啟動 | SELinux context 不對 | 見下 |

**SELinux context 修正：**

```bash
adb shell "ls -Z /system/etc/init/hiyo.rc /system/bin/hiyo_boot"
# 期望 u:object_r:system_file:s0

adb reboot recovery
adb shell "mount -o rw,remount /system_root"
adb shell "restorecon /system_root/system/etc/init/hiyo.rc"
adb shell "restorecon /system_root/system/bin/hiyo_boot"
adb shell "umount /system_root"
adb reboot
```

---

## 7. 移除 Hiyo（保留 root 與 TWRP）

```bash
adb reboot recovery
adb shell "mount -o rw,remount /system_root"
adb shell "rm -f /system_root/system/etc/init/hiyo.rc /system_root/system/bin/hiyo_boot"
adb shell "umount /system_root"
adb reboot
```

刪掉 `hiyo.rc` 這**一個檔案**，init 就不會再啟動 Hiyo。
**root 和 TWRP 都還在**，隨時可以再裝回來。

---

## 8. 一頁速查

```bash
# ═══ 階段 1–2：XDA 原文（一次性，有變磚風險，請讀原文）═══
#   Option 1（可開機）: ./fastbrick.sh → 輸入 YES
#   Option 2（變磚）:   sudo ./bootrom-step.sh + 短路測試點
#   然後:               sudo ./fastboot-step.sh   → 刷 TWRP
#   在 TWRP 內:         刷 FireOS 6 × 2 次，再刷 boot-root ZIP

# ═══ 階段 3：確認 root ═══
adb shell id                              # uid=0(root)
adb shell getprop ro.boot.unlocked_kernel # true

# ═══ 階段 4：產生 Hiyo ZIP（電腦上）═══
cd echo-dots-2-hermes && ./scripts/make-init-zip.sh

# ═══ 階段 5：推進裝置並刷入 ═══
adb push dist/hiyo-init-v1.zip /sdcard/hiyo-init-v1.zip
adb shell md5sum /sdcard/hiyo-init-v1.zip && md5sum dist/hiyo-init-v1.zip
adb shell echo "--update_package=/sdcard/hiyo-init-v1.zip" > /cache/recovery/command
adb reboot recovery                       # 看 LED：綠=成功，紅=失敗
adb reboot

# ═══ 驗證 ═══
ssh root@<echo-ip> 'md5sum /system/etc/init/hiyo.rc /system/bin/hiyo_boot'
ssh root@<echo-ip> reboot && sleep 90
ssh root@<echo-ip> 'pidof echo_probe echo_control echo_led_watchdog'

# ═══ 移除 ═══
adb reboot recovery
adb shell "mount -o rw,remount /system_root && rm -f /system_root/system/etc/init/hiyo.rc /system_root/system/bin/hiyo_boot && umount /system_root"
adb reboot
```

---

## 9. 本機對照表（已驗證一致）

| 項目 | XDA 原文／預期 | 實機 |
|---|---|---|
| 機型 | 2nd gen biscuit / RS03QR (2016) | `AEOBC` / `biscuit_puffin` |
| 韌體 | FireOS 6, `NS6574 / 7623` | Fire OS 6.5.7.4 (NS6574/7623) |
| 增量版號 | `0013121734532` | `0013121734532` |
| amonet | v2.0.0 | `refs/amonet-biscuit-v2.0.0/` |
| TWRP | 由 `fastboot-step.sh` 刷入 | TWRP 3.7.0_9-0-4b79f032 |
| 解鎖 | — | `ro.boot.unlocked_kernel=true` |
| verity | — | `ro.boot.veritymode=disabled` |
| active slot | A/B | `_a` |

### 分割區

| 分割區 | 裝置節點 | 用途 |
|---|---|---|
| `recovery` | `mmcblk0p12`（16 MB）| **TWRP 在這** |
| `misc` | `mmcblk0p8` | BCB（bootloader control block）|
| `boot_a` / `boot_b` | `mmcblk0p10` / `mmcblk0p11` | A/B kernel |
| system | `/dev/block/current-system` → `/system_root` | 唯讀（system-as-root）|

### 開機模式（原文的按鍵對應）

| 模式 | 按鍵（接電時按住）| LED |
|---|---|---|
| HACKED FASTBOOT | Volume Down | 彩虹旋轉 |
| TWRP / RECOVERY | Volume Up | 靜態白燈 |
| USBDL | MUTE（USB 要接）| 無 |

---

## 10. 為什麼「先確認可開機」不可跳過

階段 3 單獨存在，不是多餘的步驟：

1. **風險分層。** 解鎖失敗是永久變磚；刷 Hiyo 失敗只是沒生效。
2. **headless TWRP 極難除錯。** 沒有畫面，只有 LED 與 adb。
3. **工具在系統裡。** push ZIP、比對 md5、查 server IP，正常開機最方便。
4. **保留退路。** root 可用就隨時能修，不會陷入「只剩 recovery 能連」的窘境。