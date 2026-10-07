# REFERENCE — 實機量測數據

> 這些是原始 recon 數據，來自實機。要調參數或懷疑某個數字時看這裡。

---

## 1. 網路（Echo → Ubuntu）

```
ping -c 10 -i 0.3 -q 192.168.0.212   (從 Echo)
  rtt min/avg/max/mdev = 1.309/5.505/11.820/3.622 ms, 0% loss
```

上行 frame 間隔統計（連續 session）：

```
inter_mean=9.91ms  std=33.04ms
lat min=7.6  mean=39.3  max=1924.0 ms
eff_rate=16000.0Hz  ← 精準
```

**判讀**：mean 9.91 ms ≈ 10 ms frame（預期）。`std=33ms` 與 `max=1924ms` 的
尾巴不是常態——來自 WiFi power-save 週期性的喚醒延遲。若變成常態，
調 `wifi_ps`。

---

## 2. 長期穩定性（實測）

連續運作 48978 秒（13.6 小時）的累計：

```
UP frames=4897841  gaps=0  dupes=0  ooo=0  parse_errors=0
DL sent=4897827    dl_recv=4897750  dl_gaps=0  dl_dupes=0  drops=0
```

→ **零遺失、零重複、零解析錯誤。**

### 關於 `underruns` 這個數字

閒置時 log 顯示 `underruns=48099`、`q_depth` 在 6800–9000 之間浮動。
這**不是故障**：當下 `ref_rms=0` 表示 Echo 沒有在播任何東西
（沒有 session），downlink 佇列自然堆積到上限 10080。
實際播放時的數字才是有效的。不要被這個累計值誤導。

---

## 3. ALSA 拓樸

| PCM | 方向 | 參數 |
|---|---|---|
| `pcmC0D24c` | capture | 9 ch, 16000 Hz, S24_3LE, period 512, count 5 |
| `pcmC0D23p` | playback | 2 ch, 48000 Hz, S16_LE, period 2048, count 4 |

---

## 4. 通道對應

| Ch | 用途 |
|---|---|
| 0–5 | 環狀麥克風陣列 |
| **6** | **中央麥克風 — Hiyo 採用這個** |
| **7** | **硬體回音參考**（與 Ch8 相關性 1.000）|
| 8 | 硬體回音參考（冗餘）|

- Ch7 ≡ Ch8，corr = **1.000**
- 參考相對麥克風的延遲：**約 2.1 ms**
- S24_3LE → int16：`(value >> 8)` 或 EchoMuse 的 `(val*gainQ)>>20`

### 為什麼選 Ch6

先前研究比較過 Ch0–Ch6 作為 AEC 近端來源，Ch6（中央）在
「單一使用者正面對話」情境下 SNR 最穩定。7 顆全用需要 beamforming，
在 ARMv7 上不划算。

---

## 5. Speex AEC 實測

| 參數 | 值 |
|---|---|
| 取樣率 | 16000 Hz |
| tail length | 300 ms |
| ref delay | **0**（D=0 最佳）|
| frame | 160 samples (10 ms) 首選；320 (20 ms) 可接受 |
| near-end 增益變化 | **+0.01 dB**（無 gating、無 pumping）|
| live ERLE | **12 dB**（Stage B 實測）|
| CPU | ≈ **單核 3%** |

### 重要：不要套用 32–35 sample 的 ref delay 修正

量測到的 32–35 sample 偏移在 D=0 設定下**反而變差**。Speex 的 MDF
自適應濾波器已經吸收了這個延遲。這是先前研究反覆驗證的結論。

### 為什麼 mic gain 用 unity

+24 dB 的變體在正常說話音量就 clipping。unity 增益下 Speex
仍能收斂，且保留 near-end 動態。

---

## 6. 播放與回音參考的關係（最關鍵的性質）

| 播放路徑 | Ch7/8 是否有訊號 |
|---|---|
| **`pcmC0D23p`（D23, hw:0,23）** | ✅ **有，且與播放樣本對齊** |
| 主機喇叭 | ❌ 零 |
| 藍牙 | ❌ 零 |
| USB | ❌ 零 |
| `hw:0,0` (MultiMedia1) | ❌ 不是乾淨的喇叭路徑 |

實測（Stage B §B）：tinyplay 走 D23 + D24 擷取 →
Ch7/8 有訊號、corr(ch7,ch8)=1.000、lag ≈ 2.1 ms。

**推論**：Hiyo 的 downlink 必須走 D23，否則 AEC 沒有參考，回音會爆。
這也意味著 session 期間必須停掉其他播放來源。

---

## 7. mixer 獨佔流程

```
stop mixer
   ↓
等 /proc/asound/card0/pcm24c/sub0/status == "closed"
   ↓
獨佔 D24 擷取 + D23 播放（AEC 運行）
   ↓
start mixer
   ↓
驗證:
  getprop init.svc.mixer          → running
  getprop audio_mixer.init        → true
  pcm24c status                   → RUNNING (owner mixer)
  mstream1052_*                   → 重新建立
```

**恢復是強制的**：`hiyo_supervisor.sh` 在任何子程序退出或收到
TERM/INT 時都執行這段。這是不可協商的。

---

## 8. LED 硬體

| 項目 | 值 |
|---|---|
| 晶片 | IS31FL3236（36 通道 PWM）|
| 匯流排 | I2C-0（`11007000.i2c`），位址 `0x3f` |
| sysfs | `/sys/bus/i2c/devices/0-003f/frame` |
| 格式 | 72 個小寫 hex = 36 ch × 8-bit，順序 R,G,B per LED |
| LED 數 | 12（`aceLedHal_getNumLeds()` 回傳 `0xc`）|
| 方向 | index 0 → 11 **順時針** |
| 相關節點 | `boot_animation`, `led_current` |

Amazon 的 auto-brightness 會週期性覆寫，所以要先
`ledctrl -a off` + `ledctrl -c` 關掉動畫與自動亮度。

---

## 9. 喇叭 amp

```sh
/system/bin/tinymix Ext_Speaker_Amp_Switch Off   # idle 時關，否則會 hum
/system/bin/tinymix Ext_Speaker_Amp_Switch On    # 播放時開
```

---

## 10. GPT Live 契約

| 項目 | 值 |
|---|---|
| base URL | `https://api.openai.com/v1` |
| endpoint | `POST /v1/live/sessions` |
| model | `gpt-live-1` |
| voice | `marin` |
| delegation | `{"type": "client"}` |
| session id 前綴 | `live_...` |
| 計費 | **$0.05 / 分鐘 / session** |

Echo 走的是 **vendor-only** 路徑：直接建 vendor session，不建 client
WebRTC leg。憑證在 server 端解析，永不傳到裝置。

---

## 11. Seed-VC 契約

| 項目 | 值 |
|---|---|
| endpoint | `ws://127.0.0.1:8790` |
| 輸入 | 24000 Hz PCM |
| 輸出 | 22050 Hz PCM |
| block_time | 0.30 s |
| reset | 支援（`{"type":"reset"}`）|
| 參考音檔 | `.../morning_seed7_pause240.wav` |
| **+0.5 semitone 後移** | **已移除**（刻意）|

**推論**：回程延遲本質上 > 300 ms。這是既有行為，不是 bug。

---

## 12. 喚醒詞

| 項目 | 值 |
|---|---|
| 模型 | `ok_hiyo_v1_selfcontained.onnx`（205410 bytes）|
| 觸發 | 連續 4 frame 平均 > **0.70** |
| 冷卻 | 2.0 s |
| 執行環境 | `~/.venv-wakeword`（隔離）|

---

## 13. 實體按鍵 / mute

```
evdev: /dev/input/event1
```

- Action 鍵 → 手動觸發一輪
- Mute 開關 → 硬體事實，**軟體永不解除**
- `echo_control` 用 `EVIOCGRAB` 獨佔 event1

---

## 14. 已知限制

| 限制 | 說明 |
|---|---|
| ALSA 16-bit only | 內部處理另有 32-bit 路徑，但 device 端 I/O 是 16-bit |
| 必須獨佔 mixer | 目前沒有經過驗證的 libmixerAPI 共存方案 |
| 無裝置端編譯器 | 所有 C 都要 cross-compile（NDK r25c）|
| WiFi power-save | 造成尾部 jitter，需要時用 `wifi_ps` 關 |
| GPT Live 成本 | $0.05/min；--live-idle-timeout 120 是必要的剎車 |
| mixer 停止期間 Alexa 不可用 | 這是獨佔模式的必然結果 |
