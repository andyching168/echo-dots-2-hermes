# TROUBLESHOOTING — 故障與復原

> 原則：**先確認兩端狀態，再動手。** 大部分問題是「某個 service 沒起來」或
> 「mixer 沒還原」，兩者都有明確的檢查指令。

---

## 0. 三十秒診斷

```bash
cd echo-dots-2-hermes
./scripts/verify.sh --server 192.168.0.212 --echo 192.168.0.142
```

看哪一行不是 OK。

---

## 1. Ubuntu 端

### 1.1 service 沒有自動起來

```bash
loginctl show-user $USER | grep Linger      # 必須 Linger=yes
sudo loginctl enable-linger $USER
systemctl --user daemon-reload
systemctl --user list-units --type=service | grep hermes-echo
```

### 1.2 `:8792` 沒有 listen

順序問題：bridge 會等 seed-vc。

```bash
systemctl --user status hermes-echo-tts
curl -s http://127.0.0.1:8795/health
# Seed-VC is optional; inspect it only when voice conversion is enabled:
systemctl --user status hermes-seed-vc
sleep 5
systemctl --user restart hermes-echo-bridge
ss -tlnp | grep -E '879[0-9]'
```

### 1.3 一直 restart

```bash
journalctl --user -u hermes-echo-bridge -n 80 --no-pager
```

常見原因：
- venv 套件缺失 → `.venv/bin/pip install -r requirements.txt`
- `--spotify` 但 `ECHO_LIBRESPOT` 路徑不存在 → 編輯 drop-in 或移除 `--spotify`
- port 被舊 process 佔用 → `pkill -f 'echo_serv[e]r.py'` 後重啟

### 1.4 想手動跑一次看完整輸出

```bash
systemctl --user stop hermes-echo-bridge
cd ~/voice/echo-bridge
.venv/bin/python echo_server.py --host 192.168.0.212 --control --oneshot --live \
    --oneshot-tts hermes --no-oneshot-vc --led-mode exclusive --wakeword-wake
# Ctrl-C 後
systemctl --user start hermes-echo-bridge
```

---

## 2. Echo 端

### 2.1 完全沒反應 — 先看 supervisor

```bash
ssh root@192.168.0.142 'cat /data/local/tmp/hiyo_supervisor.log | tail -40'
ssh root@192.168.0.142 'for p in echo_probe echo_control echo_led_watchdog; do
    echo "$p: $(pidof $p)"; done'
```

若三個程序都不在：

```bash
ssh root@192.168.0.142 '/data/local/tmp/hiyo_supervisor.sh &'
```

### 2.2 沒有聲音

**最常見：mixer 沒被正確停止或還原。**

```bash
ssh root@192.168.0.142 '
  getprop init.svc.mixer
  cat /proc/asound/card0/pcm24c/sub0/status
  cat /proc/asound/card0/pcm23p/sub0/status
'
```

期望：跑 session 時 `mixer=stopped`、`pcm24c=RUNNING`。
閒置時 `mixer=running`。

還原 stock：

```bash
ssh root@192.168.0.142 'start mixer; start ledcontroller'
ssh root@192.168.0.142 'getprop init.svc.mixer'    # running
```

### 2.3 有回音 / AEC 失效

檢查下行是不是真的走 D23。**只有 D23 會讓 Ch7/8 有訊號。**

```bash
ssh root@192.168.0.142 'cat /proc/asound/card0/pcm23p/sub0/status'
```

若同時有藍牙/USB 播放 → 關掉。AEC 的參考只來自 D23。

### 2.4 喇叭 idle 時有 hum

喇叭 amp 沒關。Cast 模式會自動 gate，Hiyo 模式由 echo_control 管：

```bash
ssh root@192.168.0.142 '/system/bin/tinymix Ext_Speaker_Amp_Switch Off'
```

### 2.5 LED 卡住 / 不還原

`echo_led_watchdog` 每 1500 ms 檢查一次。手動強制還原：

```bash
ssh root@192.168.0.142 '
  echo STOCK > /data/local/tmp/echo_led_owner
  /data/local/tmp/echo-led --restore
'
```

### 2.6 SSH 進不去

Echo 的 dropbear 重開機後不會自己起來（除非有 persistent 設定）。
用 USB adb 重開：

```bash
adb shell '/system/bin/dropbear -R -p 22'
```

或用 `--via adb` 模式跑腳本。

---

## 3. 連線問題

### 3.1 Echo 連不上 server

```bash
# 從 Echo 測
ssh root@192.168.0.142 'ping -c 3 192.168.0.212'
# 從 server 看 firewall
sudo ufw status
sudo ss -tlnp | grep 879
```

需要開 **TCP 8792 + 8793**。

### 3.2 連上但沒有 uplink frame

伺服器 log 會顯示 `gaps` 增加：

```bash
journalctl --user -u hermes-echo-bridge -f | grep -E 'UP frames|gaps'
```

正常：`gaps=0 dupes=0 parse_errors=0 eff_rate=16000.0Hz`
（實機連續 13+ 小時都是 0）

若是 0 frames → 檢查 `echo_probe` 是否被 `mixer` 擋住（§2.2）。

### 3.3 WiFi 省電造成 jitter

實測 `inter_mean=9.91ms std=33.04ms`，偶發 max 1924 ms。

```bash
ssh root@192.168.0.142 '/data/local/tmp/wifi_ps off'
```

（`wifi_ps` 若不在裝置上，從 `refs/` 的 source 重建。）

---

## 4. 完整還原（回到 Amazon 原狀）

```bash
./scripts/uninstall.sh --server 192.168.0.212 --echo 192.168.0.142 --full
```

它會：
1. 停掉並 disable 所有 `hermes-echo-*` user unit
2. Echo：殺掉三個子程序
3. Echo：`start mixer` + `start ledcontroller` 還原 stock
4. Echo：清 `/data/local/tmp` 的 Hiyo 檔案（可保留 backups 用 `--keep-data`）
5. Echo：**偵測** `/system/etc/init/hiyo.rc` 是否存在，並印出移除步驟

### ⚠️ 第 5 點要手動完成

`--full` **無法**自動刪掉 `/system/etc/init/hiyo.rc`，因為正常開機時
`/system` 是唯讀的（system-as-root）。刪除一樣要進 recovery：

```bash
adb reboot recovery
adb shell "mount -o rw,remount /system_root"
adb shell "rm -f /system_root/system/etc/init/hiyo.rc /system_root/system/bin/hiyo_boot"
adb shell "umount /system_root"
adb reboot
```

> ⚠️ **這台是 headless TWRP**（`TW_NO_SCREEN_UI := true`），
> 沒有畫面也沒有觸控，所以**不要**照著一般教學去找「Advanced → Terminal」，
> 那個介面不存在。一律用上面的 `adb shell`。

刪掉 `hiyo.rc` 這**一個檔案**，init 就不會再啟動 Hiyo。

> 📖 完整的 recovery 操作（含進 recovery 的三種方法、出錯排查）見
> [`RECOVERY_GUIDE.md`](RECOVERY_GUIDE.md)。

**做完重開機，Echo 就是一台正常的 Amazon Echo。**

> 若你只想停用而不刪除：`--full` 不加也可以，supervisor 沒跑就沒有 Hiyo
> 行為；只是開機時 init 仍會嘗試啟動它一次（然後失敗退出）。

---

## 5. 緊急：Echo 怪怪的但我人在遠端

最安全的單一指令 —— 強制回到 stock：

```bash
ssh root@192.168.0.142 '
  killall echo_probe echo_control echo_led_watchdog 2>/dev/null
  sleep 2
  start mixer
  start ledcontroller
  echo STOCK > /data/local/tmp/echo_led_owner
  /data/local/tmp/echo-led --restore
  getprop init.svc.mixer
'
```

若連 SSH 都不行 → 拔電重開。supervisor 的設計保證它在 init 階段
就會把 stock 服務恢復。

---

## Hermes Gadget / Question Mode

目前一般 Question Mode 的正常路徑是 `echo_oneshot.py -> 127.0.0.1:8798
-> Hermes Gadget :8766`。`:8796` 只是 compatibility/recovery facade。

先看四個狀態：

```bash
systemctl --user status hermes-gateway hermes-echo-gadget hermes-echo-turn hermes-echo-bridge
hermes gadget info
ss -tln | grep -E ':8766|:8796|:8798|:8799'
```

`hermes-echo-gadget.service` 是 `echo_gadget_shadow.py` 的唯一 process owner。
不要手動再啟一份 shadow。若 :8798 掛掉：

```bash
systemctl --user restart hermes-echo-gadget
journalctl --user -u hermes-echo-gadget -n 80 --no-pager
```

新部署會產生新的 `gadget_state.json` / device id，因此第一次必須完成 Hermes
pairing。`./scripts/verify.sh` 會把 `online=true, paired=true` 視為健康狀態。

若 Gadget 真正無法恢復，`:8796` facade 才會啟動 `echo_legacy_worker.py`
(`:8799`)。這是 emergency fallback，不應在正常 Gadget 路徑常駐；切回 Gadget
後閒置 300 秒會自動回收。
