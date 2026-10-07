# PROVENANCE — 這包從哪來

> 這包不是設計文件，是**從實機快照下來的部署包**。
> 每一項都標明來源與擷取時間，方便日後比對。

---

## 1. 擷取環境

| | Server | Echo |
|---|---|---|
| Hostname | `ac-B550M-DS3H` | `echo-dot2` |
| IP | `192.168.0.212` | `192.168.0.142` |
| OS | Ubuntu 25.10 (Questing) | Fire OS 6.5.7.4 (NS6574) / Android 7.1.2 |
| Kernel | 6.17.0-41-generic | 3.18.19-gecb8cb46060-dirty (armv7l) |
| 身分 | `ac` | `root` (u:r:su:s0) |
| Uptime @擷取 | 30 days | — |

擷取工具：`ssh`（Echo 用 `root@192.168.0.142`，金鑰 `~/.ssh/id_hiyo_echo`）。

> **註**：任務描述提到「Ubuntu server」。實機是 Ubuntu 25.10，
> 不是 22.04/24.04 LTS，但 service 與 Python 相容性沒有問題。

---

## 2. 裝置端二進位 checksums（已驗證一致）

| 檔案 | MD5 | 大小 |
|---|---|---|
| `echo_probe` | `8d2b1f403345af2796a5f938b0f8e346` | 55756 |
| `echo_control` | `2785ac5c659af683d614e9d02c791261` | 20772 |
| `echo_led_watchdog` | `f5539cbac48b5448f9f6610f099ef6bc` | 6980 |
| `hiyo_supervisor.sh` | `20fff6413ca7767429feb1828a63ae37` | 8456 |
| `hiyo_boot` | `9df0f29fb96c7f2413d0be592efba69c` | 876 |
| `hiyo.rc` | `1b3fc2ff81d7ad9c334b6a100d583cb8` | 902 |

`bin/` 與 `scripts/` 的檔案是**逐 byte 從實機複製**（`ssh ... cat`），
MD5 兩端一致。這是部署時最可靠的來源。

---

## 3. 原始碼的落差（重要）

裝置端 C 原始碼散在 `refs/echo_dot2_investigation/` 的各 phase 目錄。
**不是每個二進位都有對應的、時間一致的原始碼。**

| 二進位 | 原始碼來源 | 狀態 |
|---|---|---|
| `echo_led_watchdog` | `phase5d5b/echo/echo_led_watchdog.c` | ✅ MD5 完全一致 |
| `echo_control` | `phase5d5b/echo/echo_control.c` | ⚠️ 原始碼比 binary 新（binary 是更早建的）|
| `echo_probe` | `phase5d6a/device/echo_probe_p5d6a.c` | ⚠️ 最接近的版本；binary 由 phase5f1 建立 |
| `echo_live` | `phase2/source/echo_live.c` | 早期原型（`echo_probe` 的前身）|
| `hiyo_boot` | ❌ 不在樹中 | 876 bytes 的 exec stub，原始碼極短（見下）|

`hiyo_boot` 的行為可從 binary 字串完整還原：

```c
// 概念等價：exec /data/local/tmp/hiyo_supervisor.sh via /system/bin/sh
int main(void) {
    execl("/system/bin/sh", "sh", "/data/local/tmp/hiyo_supervisor.sh", NULL);
}
```

**部署時請直接使用 `bin/` 內的二進位**，不要重新編譯，除非你確定工具鏈
（NDK r25c, ARMv7 PIE, `-pthread`）與原始碼版本相符。

---

## 4. 伺服器端來源

`server/` 是從 `192.168.0.212:~/voice/echo-bridge/` 複製，
**排除**：`.venv*`、`__pycache__`、`*.pre*`（舊備份）、`*.reverted-*`、
`captures/`、`logs/`、`p5*/`（測試 stage）、`*.wav`。

擷取時的檔案時間戳：`echo_server.py` 2026-09-20 18:58、
`echo_turn_server.py` 2026-09-21 21:20、`echo_controller.py` 2026-09-20 02:24。

`units/` 從 `~/.config/systemd/user/` 複製，含 drop-in 目錄。

---

## 5. 實機運作證據（擷取當下）

bridge 連續運作 **48978 秒（約 13.6 小時）**，統計：

```
UP frames=4897841  gaps=0  dupes=0  ooo=0  parse_errors=0
eff_rate=16000.0Hz  inter_mean=9.91ms  lat min=7.6 mean=39.3 max=1924.0 ms
DL sent=4897827   dl_recv=4897750  dl_gaps=0  dl_dupes=0  drops=0
```

→ **零遺失、零重複、取樣率精準 16000.0 Hz。**

> 唯一要注意的數字：`underruns=48099`。這是累計值且與 downlink 佇列深度
> （`q_depth` 在 6800–9000 之間浮動，上限 10080）相關——因為當下沒有
> Echo 在主動播放（`ref_rms=0`），佇列自然堆積。實際播放時不會這樣。
> 詳見 `REFERENCE.md`。

---

## 6. 這包**沒有**包含的東西

| 項目 | 為什麼 | 去哪裡拿 |
|---|---|---|
| Hermes agent 本体 | 獨立專案，體積大 | `~/.hermes/hermes-agent` |
| Seed-VC 模型 / `hermes_vc_server.py` | 獨立專案 | `~/voice/seed-vc-realtime` |
| Breeze TTS | 獨立服務 | `~/audio.cpp/...` |
| Live WebRTC bridge (:8791) | Desktop 用，Echo 不需要 | `~/voice/live-webrtc-bridge` |
| Echo root 工具 (amonet) | 刷機流程，非部署 | `refs/`、上游 GitHub |
| `.venv` 實體 | 應在新機器重建 | `requirements.txt` |
| 任何 API key | 安全 | 見 `docs/INVENTORY.md` §2 警告 |

---

## 7. 版本標記

| 元件 | 版本 |
|---|---|
| Hermes agent | `v2026.7.20-19808-g8a8bd93ef2` |
| openWakeWord | 0.6.0（主 venv）/ 0.4.0（wakeword venv）|
| 喚醒詞模型 | `ok_hiyo_v1`（selfcontained, 205410 bytes）|
| aiortc | 1.15.0 |
| onnxruntime | 1.30.0 |
| GPT Live model | `gpt-live-1`, voice `marin` |
| Seed-VC | in 24000 Hz → out 22050 Hz, block 0.30s |

---

## 8. 修正記錄：開機自啟必須走 recovery

**日期**：2026-09-21（部署包第一版之後）

第一版文件假設可以用 `mount -o rw,remount /system` 從正常開機的系統
直接寫入 init hook。**實測證明這是錯的。** 以下是更正與證據。

### 證據 1：正常開機時 /system 是唯讀的

```
$ mount | grep " / "
/dev/root on / type ext4 (ro,seclabel,relatime,data=ordered)

$ touch /system/.hiyo_test
touch: '/system/.hiyo_test': Read-only file system
```

本機是 **system-as-root + A/B**：`/system` 不是獨立分割區，而是 root image
的一部分。`ro.build.system_root_image=true`、`ro.boot.slot_suffix=_a`。

### 證據 2：Magisk 不可行

TWRP 3.7 刷 `Magisk-v27.0.zip` 的實際結果（`/cache/recovery/last_log`）：

```
- Patching ramdisk
LZMA initialization failed (5)
! Unable to patch ramdisk
Updater process ended with ERROR: 1
Error installing zip file '/sdcard/Magisk-v27.0.zip'
```

boot image 的 ramdisk 是 LZMA 壓縮，Magisk v27 的 magiskboot 處理不了。
因此沒有 Magisk，也就沒有 `/data/adb/service.d/` 可以掛 supervisor。

### 證據 3：TWRP 預設拒絕寫入

```
I:System has never been changed, leaving it read only.
```

這是 TWRP 的保護預設。它只接受「ZIP 刷入」這種明確意圖的寫入方式，
在 ZIP 的 `update-binary` 內執行 `mount -o rw,remount /system_root`
才會生效。

### 實測的 recovery 環境

| 項目 | 值 |
|---|---|
| Recovery | TWRP 3.7.0_9-0-4b79f032 |
| system 掛載點 | `/system_root`（`/dev/block/current-system`）|
| 分割區 | A/B（slot `_a`）|
| verity | disabled |
| ORS | 支援 |

### 因此的修正

| 項目 | 第一版（錯） | 現在 |
|---|---|---|
| `deploy-device.sh --enable-autostart` | `init` / `magisk` / `host-driven` | `recovery` / `host-driven` |
| 安裝方式 | SSH + remount | recovery 刷 TWRP ZIP |
| 新工具 | — | `scripts/make-init-zip.sh` |
| ZIP 設計 | — | `update-binary` 自我包含（base64 payload 內嵌）|

`update-binary` 刻意**不依賴** `unzip` / `busybox` / `mktemp`：
recovery 內可用工具不保證，payload 以 base64 直接內嵌並用 marker 區塊
定位，解碼只用 `toybox base64` 或 `base64`。

本包已在模擬環境驗證該 installer 能把兩個檔案裝到正確路徑、
權限正確，且 MD5 與來源一致（`1b3fc2ff…` / `9df0f29f…`）。

---

## 9. 2026-10-07 Hermes Gadget production refresh

Server-side Python files and systemd units were refreshed again from the live
Ubuntu host after the Hermes Gadget migration reached production. The active
Question Mode path is now:

```text
local STT -> EchoRouter -> echo_gadget_shadow.py :8798
          -> Hermes Gadget WebSocket :8766/gadget -> Hermes Agent
```

`echo_turn_server.py :8796` is only a thin compatibility/recovery facade. The
old AIAgent/MCP path lives in `echo_legacy_worker.py :8799` and is launched only
for fallback, then reaped after 300 seconds idle.

The Gadget platform integration was validated against Adolanium
`hermes-gadget-sdk` commit `d661b8f2787bc79dd48a7c399ae76d2bbc0b7d41`
(`v0.2.0-85-gd661b8f` at the time of capture). The deploy script pins the plugin
to that commit instead of following `main`.

The redistributable bundle does **not** contain the production Gadget device
key or JEV API credential. A newly deployed host creates its own
`gadget_state.json` (mode 0600) and must pair that new device with Hermes.
