# ARCHITECTURE — Hiyo Echo

> 本文件描述**目前實機正在跑**的架構，不是設計稿。所有事實都有實測依據，
> 原始量測見 `REFERENCE.md`。

---

## 1. 一張圖

```
                        ┌──────────── Ubuntu 192.168.0.212 ────────────┐
                        │                                              │
   ┌─────────────┐      │  ┌────────────────────────────────────────┐  │
   │  Echo Dot2  │      │  │ echo_server.py   (hermes-echo-bridge)  │  │
   │             │      │  │   大腦 / orchestrator                   │  │
   │ echo_control│◄─────┼──┤   ├─ wakeword.py     ok_hiyo_v1 ONNX    │  │
   │      :8793  │ ctl  │  │   ├─ echo_controller  狀態機            │  │
   │             │      │  │   ├─ echo_oneshot    一問一答           │  │
   │ echo_probe  │      │  │   ├─ echo_live       GPT Live 監督      │  │
   │      :8792  │◄────►│  │   ├─ echo_gpt        vendor-only WebRTC │  │
   │             │ PCM  │  │   ├─ echo_barge      打斷偵測           │  │
   │ echo_led_   │      │  │   ├─ echo_led/LED owner  燈光           │  │
   │   watchdog  │      │  │   └─ echo_spotify    librespot          │  │
   │             │      │  └────────────────────────────────────────┘  │
   │ hiyo_       │      │                    │                         │
   │ supervisor  │      │      ┌─────────────┼─────────────┐           │
   └─────────────┘      │      ▼             ▼             ▼           │
                        │ :8798 Gadget  :8797 STT    :8790 Seed-VC   │
                        │      │             │             │           │
                        │      ▼             ▼             ▼           │
                        │ Hermes Gadget  whisper   聲音轉換 (Hiyori)  │
                        │  :8642 / MCP   fastest   22.05kHz            │
                        │      │                                       │
                        │      ▼                                       │
                        │  :8794 admin console (127.0.0.1)             │
                        │  :8790 seed-vc   :8788 live gateway          │
                        │  :9119 dashboard :10900/10901 Breeze TTS     │
                        └──────────────────────────────────────────────┘
```

---

## 2. 兩個平面

整套分成**控制平面**與**音訊平面**，走不同 TCP 連線，互不阻塞。

### 2.1 控制平面 `:8793`（Echo → server 的意圖）

Echo 的 `echo_control` 開一個到 `192.168.0.212:8793` 的連線，送 JSON 事件：

| 事件 | 來源 | 意義 |
|---|---|---|
| `button` | GPIO / evdev `event1` | Action 鍵按下 |
| `mute` | evdev | 實體靜音開關狀態 |
| `led_ack` | LED 子系統 | 已套用燈光 frame |
| `hello` | 開機 | 帶裝置身分 / 版本 |

server 回送 LED frame 指令、音量設定、`grab` 擁有權切換。

> **實體 mute 是硬體事實，軟體永不解除。** `echo_control` 持有這個權威。

### 2.2 音訊平面 `:8792`（雙向 PCM）

單一全雙工 TCP 連線，tiny framed binary protocol（little-endian）：

```
magic u16 = 0xE0D1 | type u8 | flags u8 | seq u32 | len u16 | payload
```

| type | 方向 | 內容 |
|---|---|---|
| `0x01 UPLINK` | Echo → server | `u64 echo_mono_us` + int16 PCM **16 kHz mono** (160 smp/frame) |
| `0x02 DOWNLINK` | server → Echo | int16 PCM **48 kHz mono** (480 smp = 10 ms) |
| `0x03 METER` | Echo → server | v2 統計（ERLE / gaps / underruns / queue depth…）|
| `0x80 FLUSH` | server → Echo | 丟掉緩衝的播放（barge-in 用）|
| `0x84 HELLO` | server → Echo | `u64 server_epoch_us` |
| `0x85 HELLO_ACK` | Echo → server | `u64 echo_mono_us_at_hello` |

**為什麼是裸 PCM 而不是 Opus/WebRTC**：純 LAN，16 kHz mono = 32 kB/s。
Echo 上沒有 WS/HTTP client，裸 `socket()` 幾行就完成；server 端
`asyncio.start_server` 已經在 stack 裡。Opus/WebRTC 會帶來 ARMv7 CPU、
DTLS/SRTP、jitter buffer，在 LAN 上沒有任何好處。

---

## 3. 音訊如何在 Echo 上進出（最關鍵的一段）

Echo Dot 2 的 ALSA 拓樸：

| 裝置 | 用途 |
|---|---|
| `pcmC0D24c` | 9 通道擷取，16 kHz, S24_3LE |
| `pcmC0D23p` | 喇叭播放，48 kHz stereo |
| Ch0–Ch6 | 7 顆麥克風 |
| **Ch7 / Ch8** | **硬體回音參考（echo reference）** |

### 3.1 上行（麥克風）

```
D24 9ch 16k ──► Ch6 (中央麥克風) ──► Speex AEC ──► 16k int16 mono ──► TCP :8792
                     ▲
                     └── Ch7 (硬體參考，與麥克風樣本對齊)
```

AEC 參數是實測調出來的，**不要亂改**：

| 參數 | 值 | 說明 |
|---|---|---|
| 取樣率 | 16 kHz | 與 uplink 一致 |
| tail | 300 ms | 覆蓋房間殘響 |
| ref delay | **0** | D=0 最佳；**不要**套用實測的 32–35 sample 修正 |
| frame | 160 (10 ms) 首選，320 可接受 | |
| mic gain | **unity** | +24 dB 版本會 clipping |
| CPU | ≈ 單核 3% | |

### 3.2 下行（喇叭）— 為什麼一定要走 D23

**只有經過 `pcmC0D23p` 的播放才會出現在 Ch7/Ch8。**
走藍牙、USB、主機喇叭都不會 → AEC 立刻失去參考 → 回音爆掉。

```
TCP :8792 ──► 48k mono ──► pcmC0D23p ──► AIC3204 ──► 喇叭
                                  └──► Ch7/Ch8 硬體回音參考（~2.1 ms lag）
```

播放執行緒在 underrun 時補零，不會中斷串流。

### 3.3 mixer 的獨佔與還原

Amazon 的 `mixer` service 持有 D24/D23。要拿到乾淨路徑必須：

```
stop mixer → 等 pcm24c status = closed → 獨佔擷取 + 播放 → start mixer
```

`hiyo_supervisor.sh` 保證**任何**子程序結束或收到 TERM/INT 時都會
還原 stock mixer + stock ledcontroller。這是不可協商的 invariant。

---

## 4. 三種互動模式

| 模式 | 觸發 | 流程 | 延遲特性 |
|---|---|---|---|
| **One-shot 問答** | 「OK HiYo」喚醒 | VAD → STT :8797 → EchoRouter → Gadget shadow :8798 → Hermes Gadget :8766 → TTS → 下行 | 停頓後才送，準確優先 |
| **Live 對話** | 喚醒後進 live 模式 | 音訊直送 GPT Live（vendor-only WebRTC）→ 回聲經 Seed-VC → 下行 | 即時，但有 Seed-VC 的 ~300ms block |
| **Spotify** | 語音指令 | librespot pipe → ffmpeg 44.1k→48k → 下行 | 串流 |

### 4.1 喚醒詞

`ok_hiyo_v1` — 自訓 openWakeWord 模型（`ok_hiyo_v1.onnx`），
確認條件：連續 4 frame 平均 > 0.70，冷卻 2.0 s。

### 4.2 GPT Live 是 vendor-only

Echo 的路徑**不建 client WebRTC leg**。`echo_gpt.py` 直接建立一條
到 OpenAI `POST /v1/live/sessions` 的 session（model `gpt-live-1`,
delegation `{type: client}`），憑證只在 server 端解析，**永遠不會到裝置**。

這與 Desktop 的 GPT Live（`live-webrtc-bridge` :8791）是**兩條平行路徑**，
共用 :8790 的 Seed-VC，但互不影響。

### 4.3 Hermes Gadget（一般問答的正式主路徑）

一般 Question Mode 不再自己建立 Hermes `AIAgent`。`echo_oneshot.py` 在本地 STT 與 `EchoRouter` 完成後，直接把文字交給 `echo_gadget_shadow.py` (`127.0.0.1:8798`)；shadow 以已配對 Gadget 身分連上 Hermes Gateway 的 `:8766/gadget`。

`echo_turn_server.py :8796` 只保留 compatibility / recovery facade。若 Gadget service 真正無法恢復，才會啟動 `echo_legacy_worker.py :8799`；legacy worker 閒置 300 秒後回收。Gadget shadow 的唯一 process owner 是 `hermes-echo-gadget.service`。

裝置控制透過 Gadget actions (`echo.get_state`, `echo.get_volume`, `echo.set_volume`, `echo.play_chime`, Live mode actions 等)，physical mute 仍由 EchoController / 實體硬體權威控制，沒有 software unmute。

---

## 5. Barge-in（打斷）

```
使用者在 Echo 說話
   └─ echo_barge 在 AEC 殘餘能量上做 VAD（uplink，乾淨訊號）
        ├─ 立刻清空播放 ring（Echo 端）
        └─ 送 BARGEIN / FLUSH
             └─ server 清 ConvertedAudioTrack 佇列 + 送 Seed-VC {"type":"reset"}
```

GPT Live 本身沒有 vendor cancel event；模型靠 persona 指示「被打斷就停」。
搭配本地 flush，舊音訊不會繼續播。

---

## 6. LED 所有權

只有兩個合法 owner：**STOCK** 與 **HIYO**。

```
echo_led_watchdog (裝置端, 1500ms 週期)
   └─ 讀 echo_led_owner 檔案
        STOCK → 確保 ledcontroller 在跑、放手
        HIYO  → 搶過 frame sysfs，套用 server 投影的動畫
```

硬體是 **IS31FL3236**（I2C-0, addr 0x3f），kernel 暴露成
`/sys/bus/i2c/devices/0-003f/frame`——72 個 hex 字元 = 12 顆 RGB LED。

**watchdog 是獨立的 STOCK failover**：即使 server 掛掉，1500 ms 內交還 Amazon。

---

## 7. 為什麼這樣切

| 決策 | 理由 |
|---|---|
| Echo 當薄衛星 | ARMv7 沒有編譯器、CPU 有限；所有智慧集中在 server |
| 裸 PCM 不用 Opus | LAN 頻寬免費；省掉 ARMv7 上最貴的編解碼 |
| 播放只走 D23 | 只有它能讓 Ch7/8 產生硬體回音參考 |
| 平行新增 endpoint | 不動 Desktop GPT Live / 既有 service；可單獨重啟 |
| supervisor 必還原 stock | daemon crash 不能讓 Echo 變磚；mixer 一定要回來 |
| 不寫 /system（除了 hiyo.rc） | 完全可逆；刪一個檔就還原 |
