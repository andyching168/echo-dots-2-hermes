# INVENTORY — 服務 / port / 檔案 / 依賴

> 全部從實機擷取。要確認某個東西是什麼、在哪、聽哪個 port，看這裡。

---

## 1. Port 總表（Ubuntu 192.168.0.212）

| Port | Bind | 服務 | 用途 |
|---|---|---|---|
| **8792** | 0.0.0.0 | `echo_server.py` | **Echo 音訊隧道**（雙向 PCM）|
| **8793** | 0.0.0.0 | `echo_server.py` | **Echo 控制平面**（JSON 事件）|
| 8794 | 127.0.0.1 | `echo_server.py` | 本地 admin console（文字指令）|
| **8795** | 127.0.0.1 | `hermes-echo-tts` | Hermes `tts.provider` → Echo WAV bridge |
| 8790 | 127.0.0.1 | `hermes-seed-vc` | Seed-VC 聲音轉換 ws |
| 8791 | 127.0.0.1 | `hermes-live-webrtc-bridge` | Desktop GPT Live（**與 Echo 無關**）|
| **8766** | 0.0.0.0 | `hermes-gateway` Gadget platform | Gadget WebSocket `/gadget` |
| 8796 | 127.0.0.1 | `hermes-echo-turn` | compatibility / recovery facade |
| **8798** | 127.0.0.1 | `hermes-echo-gadget` | Echo Gadget shadow local API |
| 8799 | 127.0.0.1 | `echo_legacy_worker.py` | legacy fallback（按需啟動）|
| 8797 | 127.0.0.1 | `hermes-echo-stt` | 本地 STT |
| 8788 | 127.0.0.1 | `dev.hermes-live-voice.gateway` | Hermes Live Voice gateway |
| 8642 | 127.0.0.1 | `hermes-gateway` | Hermes agent gateway |
| 9119 | 0.0.0.0 | `hermes-dashboard` | Dashboard |
| 10900/10901 | 0.0.0.0 | Breeze TTS | TTS API |
| 7860 | 127.0.0.1 | `audiocpp_server` | audio.cpp |
| 1883 | — | Mosquitto | HA MQTT（`echo_ha_media` 用）|

**8792 / 8793 是 Echo LAN 連線；8766 是 Hermes Gadget listener，若只用本機 shadow 可綁 localhost，若要接其他實體 Gadget 則維持 LAN 可達。**

---

## 2. systemd user units

| Unit | 對應腳本 | 說明 | Restart |
|---|---|---|---|
| `hermes-echo-bridge.service` | `echo_server.py` | **主服務**：control + oneshot + live + vc + spotify | on-failure |
| `hermes-echo-tts.service` | `echo_hermes_tts.py` | Hermes-configured TTS localhost bridge :8795 | on-failure |
| `hermes-echo-gadget.service` | `echo_gadget_shadow.py` | **Gadget adapter 唯一 owner**，連 Hermes :8766、提供本機 :8798 | on-failure |
| `hermes-echo-turn.service` | `echo_turn_server.py` | :8796 compatibility/recovery facade；必要時啟動 legacy worker | on-failure |
| `hermes-echo-stt.service` | `echo_stt_server.py` | 本地 STT（Hermes venv，fastest-whisper 常駐）| on-failure |
| `hermes-echo-ha-media.service` | `echo_ha_media.py` | Home Assistant media_player (MQTT discovery) | always |
| `hermes-seed-vc.service` | `hermes_vc_server.py` | 聲音轉換（**Hermes 本体，非本包**）| — |
| `hermes-gateway.service` | Hermes CLI | agent gateway（**非本包**）| — |
| `hermes-dashboard.service` | Hermes | dashboard（**非本包**）| — |
| `dev.hermes-live-voice.gateway.service` | node | Desktop live voice（**非本包**）| — |

### Drop-ins（覆寫，要注意）

| 檔案 | 作用 |
|---|---|
| `units/examples/spotify.conf` | 選用 Spotify/`--no-barge` 範例，不會預設安裝 |
| `hermes-echo-turn.service.d/align.conf` | 對齊 Hermes：不排除 toolset、連所有 MCP、90 iterations |
| `hermes-echo-turn.service.d/jev.conf` | JEV gate（含 API key，**部署時要換掉**）|

> `jev.conf` 在可發佈版本中不含 key，且預設停用 JEV；需要時用本機 override 加入自己的憑證。

---

## 3. 主服務啟動參數（公開預設）

```
echo_server.py
  --host 192.168.0.212
  --control
  --oneshot
  --oneshot-tts hermes        沿用 Hermes tts.provider
  --no-oneshot-vc             預設不要求 Seed-VC
  --led-mode exclusive
  --wakeword-wake
  --outdir .../captures
```

GPT Live、Seed-VC、Spotify 都是選用功能；完整組合見 `VOICE_OPTIONS.md`。`units/examples/hiyo-full-voice.conf` 保存原 Hiyo production-style 範例。

---

## 4. `server/` 模組角色

| 檔案 | 角色 |
|---|---|
| `echo_server.py` | 主程式：TCP 隧道、PCM 框架、downlink pacing、source 選擇、所有模式的 orchestrator |
| `echo_controller.py` | **權威狀態機**：Echo 互動狀態 + 實體控制仲裁（與 GPT/Hermes 解耦）|
| `echo_oneshot.py` | 一問一答生命週期：IDLE → LISTEN → VAD → STT → router |
| `echo_qsession.py` | 有界對話 session 管理（裝置狀態 vs Hermes session 分離）|
| `echo_hermes_tts.py` | 以 Hermes Agent 環境呼叫 `text_to_speech_tool`，提供 localhost :8795 WAV API |
| `echo_gadget_shadow.py` | Gadget protocol adapter + actions/sensors + localhost :8798 turn API |
| `echo_turn_server.py` | :8796 薄 compatibility/recovery facade |
| `echo_legacy_worker.py` | :8799 舊 AIAgent / MCP / QuestionSessionManager fallback（按需啟動） |
| `echo_stt_server.py` | :8797 STT server（模型常駐）|
| `echo_live.py` | Live 模式監督：controller ↔ `echo_gpt` |
| `echo_gpt.py` | vendor-only GPT Live WebRTC（憑證 server 端解析）|
| `echo_barge.py` | Barge-in 偵測（AEC 上行能量 VAD）|
| `echo_trace.py` | 選用 JSONL 診斷（`ECHO_TRACE` 未設時完全 inert）|
| `echo_hermes.py` | Hermes ↔ Echo 的 runtime context + mode router + tool facade |
| `echo_led.py` | LED 狀態投影 + 動畫 renderer |
| `echo_led_owner.py` | LED 擁有權決策（STOCK vs HIYO）|
| `echo_spotify.py` | Spotify Connect（librespot pipe → ffmpeg → 48k）|
| `echo_ha_media.py` | Home Assistant MQTT media_player |
| `wakeword.py` | ok_hiyo 喚醒詞推論 |
| `admin_8794.py` | :8794 本地 admin console |
| `volume_frames.py` | 音量 frame 產生 |

### 測試 / 診斷（非執行時必需）

`cooldown_test.py`, `offline_test.py`, `state_machine_test.py`,
`led_demo.py`, `led_admin.py`, `led_integration_test.py`,
`oneshot_led_timeline_test.py`, `p5c_*.py`, `p5d_*.py`, `p5f1_*.py`

---

## 5. Python 環境

| Venv | 用途 | 關鍵套件 |
|---|---|---|
| `~/voice/echo-bridge/.venv` | 主服務 | aiohttp 3.14, aiortc 1.15, **openwakeword 0.6.0**, onnxruntime 1.30, numpy 2.4, paho-mqtt 2.1, av 17.1 |
| `~/voice/echo-bridge/.venv-wakeword` | 喚醒詞隔離 | onnxruntime, numpy, openwakeword 0.4, scipy, sklearn, soundfile |
| `~/.hermes/hermes-agent/venv` | Hermes turn/STT | fastest-whisper, Hermes 本体 |

> ⚠️ 主服務的 `--wakeword` 需要 `openwakeword`；目前 `.venv` 已含 0.6.0，
> 所以主服務直接跑。`.venv-wakeword` 是早期隔離版本。

---

## 6. Echo 端（`/data/local/tmp`）

| 檔案 | 大小 | 角色 |
|---|---|---|
| `echo_probe` | 55756 | **音訊隧道 client**：D24 擷取 + Speex AEC + D23 播放 + TCP :8792 |
| `echo_control` | 20772 | **控制 client**：按鍵 evdev、mute、LED frame、音量、:8793 |
| `echo_led_watchdog` | 6980 | **STOCK failover**：1500 ms 週期檢查 LED 擁有權 |
| `hiyo_supervisor.sh` | 8456 | 擁有上述三個子程序；保證還原 stock mixer |
| `echo-led` | 4297 | LED ring 工具（`--all` / `--led` / `--off` / `--restore`）|
| `echo-cast` | 4961 | Google Cast receiver 控制（選用）|

### 開機自啟（`/system`）

| 檔案 | 角色 |
|---|---|
| `/system/etc/init/hiyo.rc` | additive init 服務定義（刪此檔即還原）|
| `/system/bin/hiyo_boot` | 876-byte stub，`exec` supervisor |

> ⚠️ **這兩個檔案只能從 recovery 寫入。** 正常開機時 `/system` 是唯讀的
> （system-as-root：`/dev/root on / type ext4 (ro,seclabel,relatime,data=ordered)`），
> `mount -o rw,remount /system` 會失敗。Magisk 也不可行（v27 無法 patch
> LZMA ramdisk）。安裝用 `scripts/make-init-zip.sh` 產生的 TWRP ZIP，
> 詳見 `QUICKSTART.md` §5。

### Recovery 環境（實測）

| 項目 | 值 |
|---|---|
| Recovery | **TWRP 3.7.0_9-0-4b79f032** |
| system 掛載點 | `/system_root`（`/dev/block/current-system`）|
| 分割區配置 | A/B（`ro.boot.slot_suffix=_a`）|
| verity | `ro.boot.veritymode=disabled` |
| TWRP 預設 | **read-only**（"System has never been changed, leaving it read only"）|
| 覆寫方式 | ZIP 內 `mount -o rw,remount /system_root` |
| ORS | 支援（`/cache/recovery/command` 的 `--update_package=`）|
| 已驗證的失敗 | Magisk-v27.0.zip → `LZMA initialization failed (5)` → ERROR: 1 |

---

## 7. 硬體對應（Echo Dot 2 / RS03QR）

| 項目 | 值 |
|---|---|
| SoC | MediaTek MT8163（ARMv7 Cortex-A7）|
| 型號代號 | `AEOBC` / `biscuit_puffin` |
| ALSA card | `card0` |
| 擷取 | `pcmC0D24c` — 9ch, 16 kHz, S24_3LE |
| 播放 | `pcmC0D23p` — 2ch, 48 kHz, S16_LE |
| Codec | TLV320AIC3204 |
| 麥克風 | Ch0–Ch6（7 顆）|
| 回音參考 | **Ch7 / Ch8** |
| LED driver | IS31FL3236 @ I2C-0 0x3f → `frame` sysfs |
| LED 數量 | 12 顆 RGB（index 0→11 順時針）|
| 喇叭 amp | `Ext_Speaker_Amp_Switch`（tinymix）|

---

## 8. 外部依賴

| 依賴 | 用途 | 必要性 |
|---|---|---|
| OpenAI API key | GPT Live (`gpt-live-1`) | Live 模式才需要 |
| Hermes agent + Gadget plugin | 一般問答、session、actions；Gadget plugin 綁 :8766 | One-shot 需要 |
| Seed-VC (:8790) | 選用聲音轉換 | `--oneshot-vc` / `--live-vc` 才需要 |
| Hermes TTS | Normal Question Mode 語音合成 | 公開預設；由 `tts.provider` 決定實際 backend |
| Breeze TTS | 原 Hiyo direct TTS backend | 只有 `--oneshot-tts direct` 才需要 |
| Mosquitto MQTT | HA 整合 | 選用 |
| librespot | Spotify | `--spotify` 才需要 |
| Home Assistant | media_player | 選用 |

---

## 9. 路徑假設與改寫

`units/` 內的單元檔保留**實機擷取的絕對路徑**（`/home/ac`,
`/home/ac/voice/echo-bridge`）。這是刻意的——它們是「已知可運作」的證據。

`scripts/deploy-server.sh` 在安裝時會自動改寫：

| 原值 | 改寫為 |
|---|---|
| `/home/ac/voice/echo-bridge` | 目標的 `--dir`（預設 `~/voice/echo-bridge` 的絕對路徑）|
| `/home/ac` | 目標的 `$HOME` |
| `--host 192.168.0.212` | 目標主機的第一個 IP（若不同）|

**不會**被改寫的（刻意保留，因為它們指向 Hermes 本体）：

- `~/.hermes/hermes-agent`（Hermes agent 安裝位置）
- `~/librespot-git`（Spotify，選用）
- `:8790` Seed-VC 位址

若你的 Hermes 裝在別的地方，安裝後手動改：

```sh
systemctl --user edit hermes-echo-turn    # 覆寫 PYTHONPATH / ExecStart
systemctl --user edit hermes-echo-stt
```

---

## 10. JEV gate（安全預設）

公開部署包不含 `TYPESAFE_API_KEY`，`units/hermes-echo-turn.service.d/jev.conf` 預設 `ECHO_JEV_GATE=0`。若你有自己的 TypeSafe API key，可用 systemd override 啟用：

```sh
systemctl --user edit hermes-echo-turn
# 加入：
# Environment=TYPESAFE_API_KEY=<your-key>
# Environment=ECHO_JEV_GATE=1
```

未啟用 JEV 不影響 Gadget 主路徑；legacy HA skill gate 會依 worker 的既有 fail-open 行為處理。
