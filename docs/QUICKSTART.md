# QUICKSTART — 從刷好機的 Echo 到能說話

> 假設：你手上有一台**剛 root 完**的 Echo Dot 2，一台跑 Ubuntu 的 server，
> 兩台在同一個 LAN。目標是讓 Echo 變成 Hiyo 語音衛星。

全程約 30–45 分鐘，其中大部分是等 root 與等 service 起來。

---

## 0. 前置條件檢查

### 0.1 Ubuntu server

| 需求 | 檢查指令 | 本包實測值 |
|---|---|---|
| Ubuntu 22.04+ | `lsb_release -a` | Ubuntu 25.10 |
| Python 3.11+ | `python3 -V` | 3.11 / 3.13 |
| systemd user + linger | `loginctl show-user $USER \| grep Linger` | `Linger=yes` |
| 靜態 IP（強烈建議） | `ip -4 addr` | `192.168.0.212` |
| 對外網路（OpenAI / Hermes） | `curl -sI https://api.openai.com` | 需要 |

**linger 一定要開**，否則 user service 不會在開機時自動起來：

```bash
sudo loginctl enable-linger $USER
loginctl show-user $USER | grep Linger     # 要看到 Linger=yes
```

### 0.2 Echo Dot 2

| 需求 | 檢查指令（USB 或網路 adb） | 本包實測值 |
|---|---|---|
| 已 root | `adb shell id` → `uid=0(root)` | `u:r:su:s0` |
| 有網路 | `adb shell ip -4 addr show wlan0` | `192.168.0.142` |
| SSH 進得去 | `ssh root@<echo-ip> id` | 可（dropbear） |
| ARMv7 little-endian | `adb shell uname -m` | `armv7l` |
| Fire OS 6 / Android 7.1 | `adb shell getprop ro.build.version.name` | `Fire OS 6.5.7.4` |

Echo 的 root 方式（amonet-biscuit）不在本包範圍；本包從**已經有 root shell**開始。
若你的機器還沒 root，先看 `refs/` 裡的 amonet 資料。

### 0.3 先讓 Echo 能 SSH

Echo 內建 dropbear，但**預設沒開**。用 USB adb 開一次：

```bash
# 把本包的公鑰推上去（本包用 id_hiyo_echo）
adb push ~/.ssh/id_hiyo_echo.pub /data/local/tmp/
adb shell '
  mkdir -p /data/ssh
  cat /data/local/tmp/id_hiyo_echo.pub >> /data/ssh/authorized_keys
  chmod 700 /data/ssh; chmod 600 /data/ssh/authorized_keys
  # 啟動 dropbear（開機後要重做，除非用第 5 節的方法）
  /system/bin/dropbear -R -p 22
'
# 找到 IP 後測試
ssh -i ~/.ssh/id_hiyo_echo root@<echo-ip> id
```

> 若 SSH 連不上但 adb 可以，本包所有 device 腳本都有 `--via adb` 模式，
> 不需要 SSH 也能部署。

---

## 1. Ubuntu 端：一鍵部署

```bash
cd echo-dots-2-hermes
./scripts/deploy-server.sh --host 192.168.0.212 --user ac
```

它會依序做完：

1. 建 `~/voice/echo-bridge/`，複製 `server/` 全部內容
2. 建立 `.venv` 並安裝 `requirements.txt`（含 openwakeword / onnxruntime / aiortc / paho-mqtt）
3. 安裝 Hermes Gadget platform plugin（固定到本包驗證過的 SDK commit）與 legacy-only `hermes-echo` plugin
4. 設定 Gadget listener `0.0.0.0:8766`，安裝 `units/*.service`（含獨立 `hermes-echo-gadget.service`）與 drop-in
5. `systemctl --user daemon-reload` → `enable --now` 全部 unit，並確認 linger
6. 跑 `scripts/verify.sh` 檢查 service、Gadget pairing 與 ports

**這步做完，Ubuntu 大腦就已經在等 Echo 連進來了。**

### 1.1 手動版（想逐步確認時）

```bash
mkdir -p ~/voice/echo-bridge && cd ~/voice/echo-bridge
rsync -a <this-bundle>/server/ ./
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
mkdir -p ~/.config/systemd/user
cp <this-bundle>/units/*.service ~/.config/systemd/user/
cp -r <this-bundle>/units/*.service.d ~/.config/systemd/user/
systemctl --user daemon-reload
# Hermes Gadget platform（Hermes CLI 已安裝的前提）
hermes plugins install https://github.com/Adolanium/hermes-gadget-sdk/tree/d661b8f2787bc79dd48a7c399ae76d2bbc0b7d41/plugin --enable
hermes config set platforms.gadget.enabled true
hermes config set platforms.gadget.extra.host 0.0.0.0
hermes config set platforms.gadget.extra.port 8766
cp -a <this-bundle>/hermes-plugins/hermes-echo ~/.hermes/plugins/

systemctl --user enable --now hermes-echo-tts hermes-echo-bridge hermes-echo-gadget \
    hermes-echo-turn hermes-echo-stt hermes-echo-ha-media
systemctl --user status hermes-echo-bridge
```

> `hermes-gateway` / `hermes-dashboard` 屬於 Hermes 本体。公開預設的 normal Question Mode 透過 `hermes-echo-tts` 沿用 Hermes `tts.provider`；Seed-VC 與 GPT Live 都是可選。完整組合見 `VOICE_OPTIONS.md`。

---

## 2. Echo 端：一鍵部署

```bash
./scripts/deploy-device.sh --host 192.168.0.142 --server 192.168.0.212
```

它會：

1. 建立 `/data/local/tmp` 下的目錄結構
2. 把 `device/bin/` 三個執行檔推上去並 `chmod 755`
3. 把 `device/scripts/` 推上去
4. 把 `device/init/hiyo.rc` + `device/bin/hiyo_boot` 裝進 `/system`（見第 5 節，可選）
5. 設定 `ANDROID_ROOT` / `ANDROID_DATA` 相關的環境
6. 啟動 supervisor，並驗證三個子程序都活著

### 2.1 手動版

```bash
H=192.168.0.142
ssh root@$H 'mkdir -p /data/local/tmp'
scp device/bin/* root@$H:/data/local/tmp/          # 若無 sftp，見下方註記
ssh root@$H 'chmod 755 /data/local/tmp/echo_* /data/local/tmp/hiyo_boot'
ssh root@$H 'cp /data/local/tmp/hiyo_supervisor.sh /data/local/tmp/ && chmod 755 ...'
```

> **注意**：Echo 的 dropbear 沒有 `sftp-server`，`scp` 會失敗。
> 本包腳本改用 `ssh ... 'cat > file'` 串流，這是實測可用的方式。

---

## 3. 驗證

```bash
./scripts/verify.sh --server 192.168.0.212 --echo 192.168.0.142
```

期望輸出（本包實測）：

```
[server] hermes-echo-bridge      active (running)   :8792 listening
[server] hermes-echo-tts         active (running)   :8795 Hermes-configured TTS
[server] hermes-echo-gadget      active (running)   :8798 online+paired
[server] hermes-echo-turn        active (running)   :8796 compatibility facade
[server] hermes-echo-stt         active (running)   :8797 listening
[server] hermes-echo-ha-media    active (running)
[server] hermes-seed-vc          optional           :8790 (only when voice conversion is enabled)
[echo]   echo_control            running            (pid ...)
[echo]   echo_probe              running            (pid ...)
[echo]   echo_led_watchdog       running            (pid ...)
[echo]   mixer                   running            (stock restored)
[link]   uplink                 frames flowing, 0 gaps / 0 dupes
```

---

## 4. 實際操作 Echo

| 動作 | 結果 |
|---|---|
| 說「OK HiYo」 | LED 亮起，進入聆聽 |
| 說完一句話 | 停頓後自動送 STT → EchoRouter → Hermes Gadget → 回答 |
| 按 Action 鍵 | 手動觸發一輪（不依賴喚醒詞）|
| 按 mute 鍵 | 硬體靜音，LED 變紅；**不會被軟體解除** |
| 說「OK HiYo，播放 Spotify」 | 走 Spotify Connect 下行 |

---

## 5. 開機自啟：**必須進 recovery 刷 ZIP**

> ⚠️ **這一節是實測後修正的結論。** 原本以為可以用
> `mount -o rw,remount /system` 從正常開機的系統直接寫入 —
> **這是錯的，在 RS03QR 上做不到。** 實際必須進 recovery 用 ZIP 刷入。
>
> 📖 **完整的逐步教學（含解鎖／TWRP 的前置階段）見
> [`RECOVERY_GUIDE.md`](RECOVERY_GUIDE.md)。** 本節只講「為什麼」與摘要。

### 5.1 為什麼正常開機時改不動

本機是 **system-as-root + A/B** 架構，`/system` 不是獨立分割區，
而是 root image 的一部分：

```
$ mount | grep ' / '
/dev/root on / type ext4 (ro,seclabel,relatime,data=ordered)
                                        ^^ 唯讀
```

```
$ touch /system/.test
touch: '/system/.test': Read-only file system
```

即使 root，`mount -o rw,remount /system` 也不會成功。舊版
`deploy-device.sh` 的 `--enable-autostart init` 走的就是這條路，**已經移除**。

### 5.2 為什麼 Magisk service.d 也不行

TWRP 3.7 實測刷 `Magisk-v27.0.zip` 會失敗（見 `/cache/recovery/last_log`）：

```
- Patching ramdisk
LZMA initialization failed (5)
! Unable to patch ramdisk
Updater process ended with ERROR: 1
```

boot image 的 ramdisk 是 LZMA 壓縮，Magisk v27 的 magiskboot 處理不了。
**所以沒有 Magisk，也就沒有 `/data/adb/service.d/`。** 這條路在 RS03QR 上不可用。

### 5.3 正確做法：recovery + TWRP ZIP

唯一可行的方法是把 `hiyo.rc` 與 `hiyo_boot` 包成一個 TWRP 可刷的 ZIP，
從 recovery 刷進去。

**步驟：**

```bash
# 1. 產生 ZIP（本包已附，也可重新產生）
./scripts/make-init-zip.sh
#   → dist/hiyo-init-v1.zip
```

```bash
# 2. 推上裝置
ssh root@<echo-ip> 'cat > /sdcard/hiyo-init-v1.zip' < dist/hiyo-init-v1.zip
#   或用 adb push（沒有 sftp-server，scp 會失敗）
adb push dist/hiyo-init-v1.zip /sdcard/
```

```bash
# 3. 重開進 recovery
adb reboot recovery
#   或： ssh root@<echo-ip> reboot recovery
```

```bash
# 4. 在 recovery 內刷入（兩條路，選一）
#
# 4a. ORS（免手動操作，推薦）— TWRP 支援 /cache/recovery/command
adb shell 'echo "--update_package=/sdcard/hiyo-init-v1.zip" > /cache/recovery/command'
adb reboot recovery
#
# 4b. 手動：TWRP 主畫面 → Install → 選 hiyo-init-v1.zip → 滑動確認
```

```bash
# 5. 重開回系統
adb reboot
```

> 📖 **完整的逐步操作手冊（含出錯排查、一頁速查）見
> [`RECOVERY_GUIDE.md`](RECOVERY_GUIDE.md)。**
> 第一次做建議直接看那份，本節只是摘要。

```bash
# 6. 驗證
ssh root@<echo-ip> 'ls -l /system/etc/init/hiyo.rc /system/bin/hiyo_boot'
ssh root@<echo-ip> 'getprop init.svc.mixer'
```

### 5.4 ZIP 裡做了什麼

```
META-INF/com/google/android/update-binary   ← 掛載 system 為 rw、複製、設權限、還原
META-INF/com/google/android/updater-script  ← "#MAGISK" 風格的 no-op stub
hiyo.rc                                      ← 複製到 /system/etc/init/
hiyo_boot                                    ← 複製到 /system/bin/ (755)
```

`update-binary` 的關鍵動作：

```sh
mount -o rw,remount /system_root     # 在 recovery 內才有效
cp hiyo.rc /system_root/system/etc/init/hiyo.rc
cp hiyo_boot /system_root/system/bin/hiyo_boot
chmod 755 /system_root/system/bin/hiyo_boot
chmod 644 /system_root/system/etc/init/hiyo.rc
restorecon ...                       # 修正 SELinux context
umount /system_root
```

> 在 recovery 內，`/system_root` 就是那個唯讀 image 被掛成可寫的掛載點。
> TWRP 預設會說「System has never been changed, leaving it read only」——
> 那是**TWRP 自己的保護預設**，ZIP 內的 `mount -o rw,remount` 可以覆寫它。

### 5.5 解除安裝

同一個 ZIP 帶 `--uninstall` 參數，或在 recovery 內直接：

```sh
mount -o rw,remount /system_root
rm -f /system_root/system/etc/init/hiyo.rc /system_root/system/bin/hiyo_boot
umount /system_root
```

刪掉 `hiyo.rc` 這一個檔案，init 就不會再啟動 Hiyo。**完全可逆。**

### 5.6 替代方案：主機驅動（不動裝置，但只在主機在線時有效）

若你暫時不想進 recovery：

```bash
./scripts/deploy-device.sh --host <echo> --enable-autostart host-driven
```

由主機的 systemd unit 等 adb/ssh 出現後再拉起 supervisor。
**重開機後如果主機不在線，Echo 就只是一台普通 Echo。** 適合開發，不適合正式部署。

---

## 6. 常見第一次失敗

| 症狀 | 原因 | 解法 |
|---|---|---|
| service 不自動起來 | `linger` 沒開 | `sudo loginctl enable-linger $USER` |
| `:8795` 沒 listen | Hermes TTS bridge 未啟動 | `systemctl --user restart hermes-echo-tts`；再看 `/health` |
| Seed-VC 連線失敗 | 啟用了 voice conversion 但 :8790 沒起來 | 啟動 Seed-VC，或改用 `--no-oneshot-vc` / `--no-live-vc` |
| Echo 連不上 server | server firewall | 開 TCP 8792/8793 |
| Echo 沒聲音 | `mixer` 沒被正確停止/還原 | 看 `docs/TROUBLESHOOTING.md` §2 |
| 喚醒詞不觸發 | 用了錯的 venv | 要 `.venv-wakeword`（見 INVENTORY）|
| LED 沒反應 | `ledcontroller` 被搶走 | `echo_led_watchdog` 會在 1500ms 內還原 |

完整排查見 `docs/TROUBLESHOOTING.md`。
