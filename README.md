# echo-dots-2-hermes

> **Independent project.** `echo-dots-2-hermes` is an interoperability / hardware-reuse project for
> Amazon Echo Dot 2 hardware and Hermes Agent. It is not affiliated with or endorsed
> by Amazon, Nous Research, or the Hermes Gadget SDK project.
>
> Before publishing a fork or release, read [`PUBLICATION_AUDIT.md`](PUBLICATION_AUDIT.md)
> and [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

> 把這包交給一個「手上只有剛刷好機的 Echo Dot 2」的人，他就能把整套 Hiyo 語音助理
> 架起來。所有內容都是從**目前正在運作的實機**上抓下來的，不是設計稿。

---


## License

Project source code is released under the [MIT License](LICENSE).
The project source code is MIT-licensed. The project-specific `ok_hiyo_v1` wake-word model is separately licensed under CC BY-NC-SA 4.0; see [MODEL_LICENSE.md](MODEL_LICENSE.md). Third-party components and model assets keep their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## 0. 這包是什麼

Amazon Echo Dot 2nd Gen（RS03QR / biscuit_puffin）被改成一台 **Hiyo 語音衛星**：

```
   Echo Dot 2 (客廳)                         Ubuntu server 192.168.0.212
   ┌──────────────────────┐                 ┌────────────────────────────────┐
   │ echo_control  :8793  │◄──── control ───│ echo_server.py (Hiyo 大腦)      │
   │ echo_probe    :8792  │◄──── PCM   ────►│   ├─ wakeword  ok_hiyo_v1       │
   │ echo_led_watchdog    │                 │   ├─ STT       :8797            │
   │ hiyo_supervisor.sh   │                 │   ├─ Gadget    :8798 ─► Hermes Gadget │
                                            │   ├─ Hermes TTS :8795            │
   └──────────────────────┘                 │   ├─ GPT Live / Seed-VC 聲音    │
        LED ring / 麥克風 / 喇叭             │   └─ Spotify / HA media         │
                                            └────────────────────────────────┘
```

- Echo 只當**薄衛星**：收音（AEC）、播放、LED、實體按鍵、mute。
- 所有智慧都在 Ubuntu：喚醒詞、STT、Hermes Agent、Hermes-configured TTS、可選 Seed-VC / GPT Live / Spotify。
- Echo 的執行檔全部在 `/data/local/tmp`，可以整包刪掉還原。
- 公開預設直接沿用 Hermes 本身的 `tts.provider`；不強迫安裝 Breeze / Seed-VC。原始 Hiyo 直連 TTS + Seed-VC 路徑仍可選。
- 開機自啟需要**一個** additive 檔案 `/system/etc/init/hiyo.rc`（+ 876-byte
  `hiyo_boot` stub）。**只有這兩個檔案在 `/system`，刪掉即完全還原。**
  安裝必須進 recovery，見 `docs/RECOVERY_GUIDE.md`。

---

## 1. 三分鐘總覽

| 我想… | 去哪 |
|---|---|
| 從零開始把一台新 Echo 接上線 | `docs/QUICKSTART.md` |
| **從未解鎖到裝好的完整流程** | `docs/RECOVERY_GUIDE.md` |
| XDA 解鎖/TWRP 上游參考 | `docs/reference/XDA_ROOT_REFERENCE.md` |
| 搞懂整體架構與資料流 | `docs/ARCHITECTURE.md` |
| 不要 Live / 不要 Seed-VC / 使用原聲 | `docs/VOICE_OPTIONS.md` |
| 知道每個服務／port／檔案的角色 | `docs/INVENTORY.md` |
| 遇到問題要排查 | `docs/TROUBLESHOOTING.md` |
| 知道這包從哪台機器、哪個版本抓的 | `docs/PROVENANCE.md` |
| 只想要指令 | `scripts/` |

---

## 2. 目錄結構

```
echo-dots-2-hermes/
├── README.md                  ← 你現在看的
├── docs/
│   ├── QUICKSTART.md          從刷機到能說話的完整步驟
│   ├── RECOVERY_GUIDE.md      ★ 解鎖 → TWRP → 刷 Hiyo 的完整流程
│   ├── ARCHITECTURE.md        架構、資料流、通訊協定
│   ├── VOICE_OPTIONS.md       TTS / Seed-VC / Live 可選組合
│   ├── INVENTORY.md           服務 / port / 檔案 / 依賴總表
│   ├── TROUBLESHOOTING.md     常見故障與復原
│   ├── PROVENANCE.md          這包的來源與版本
│   ├── REFERENCE.md           實機量測數據（原始 recon）
│   └── reference/
│       └── XDA_ROOT_REFERENCE.md  XDA 上游解鎖/TWRP 參考
├── server/                    Ubuntu 上的 Hiyo 大腦（可直接部署）
│   ├── echo_server.py         主程式（control/oneshot/live/vc/barge/spotify）
│   ├── echo_turn_server.py    Hermes 對話橋  :8796
│   ├── echo_stt_server.py     STT           :8797
│   ├── echo_ha_media.py       Home Assistant media_player (MQTT)
│   ├── wakeword.py            ok_hiyo 喚醒詞
│   ├── wakeword_models/       喚醒詞 ONNX 模型
│   ├── test_audio/            測試音檔
│   └── ... (模組見 INVENTORY.md)
├── units/                     systemd user units + drop-ins
├── hermes-plugins/            legacy-only hermes-echo plugin
├── device/                    Echo 端的全部內容（已備份，可直接推回）
│   ├── bin/                   ARMv7 執行檔
│   ├── scripts/               裝置端 shell（supervisor / LED / cast）
│   ├── init/                  hiyo.rc + update-binary.tpl（開機自啟）
│   └── src/                   C 原始碼
├── dist/                      hiyo-init-v1.zip（recovery 用）
└── scripts/
    ├── deploy-server.sh       一鍵部署 Ubuntu 端
    ├── deploy-device.sh       一鍵部署 Echo 端（SSH/adb）
    ├── make-init-zip.sh       產生 recovery 用的 TWRP ZIP
    ├── verify.sh              兩端健康檢查
    └── uninstall.sh           完整還原
```

---

## 3. 目前的實機狀態（擷取時間見 PROVENANCE.md）

| 角色 | 主機 | 位址 | 身分 |
|---|---|---|---|
| 大腦 | `ac-B550M-DS3H` | `192.168.0.212` | Ubuntu 25.10, kernel 6.17, user `ac` |
| 衛星 | `echo-dot2` | `192.168.0.142` | Fire OS 6.5.7.4 (NS6574) / Android 7.1.2, root |

Echo 端已 root（amonet + TWRP），SSH 以 `root@192.168.0.142` 直接進入，
金鑰 `~/.ssh/id_hiyo_echo`。

---

## 4. 這包「已經可以動」的部分

- Ubuntu 端 Echo services 皆由 systemd user 管理；`hermes-echo-gadget.service` 獨立擁有 Gadget shadow，`linger=yes` → 開機自啟。
- Echo 端有一條 AEC 上行 / 下行隧道，實測連續 13+ 小時 **0 gap / 0 dup**。
- 喚醒詞、STT、Hermes 對話、GPT Live、Seed-VC 聲音轉換、Spotify、HA media_player 都已接好。

## 5. Echo 開機自啟：必須進 recovery 刷 ZIP

Echo 端的開機自啟靠裝置上的 `/system/etc/init/hiyo.rc`（+ `/system/bin/hiyo_boot`）。

**但這兩個檔案沒有辦法從正常開機的系統寫進去。** 實測結論：

| 方法 | 結果 |
|---|---|
| `mount -o rw,remount /system` | ❌ 失敗 — `/system` 在 system-as-root 的唯讀 image 裡 |
| Magisk `service.d` | ❌ 失敗 — Magisk v27 無法 patch 本機 boot（LZMA ramdisk）|
| **recovery + TWRP ZIP** | ✅ **唯一可行** |

所以正確流程是：產生 ZIP → 推上裝置 → 重開進 recovery → 刷入。
`scripts/make-init-zip.sh` 會產生自帶 payload 的 ZIP，
`scripts/deploy-device.sh --enable-autostart recovery` 會把它推上去並印出後續步驟。

完整說明與 ORS 自動刷法見 `docs/QUICKSTART.md` §5。

---

## 6. 發佈前安全檢查

部署包已移除實機 JEV 憑證；`jev.conf` 預設 `ECHO_JEV_GATE=0`。
若要啟用 JEV，請在自己的 systemd override 中提供 `TYPESAFE_API_KEY`。
仍建議發佈前檢查 server IP、額外 override 與任何自行加入的憑證：

交付給別人之前先跑：

```sh
grep -rniE "api[_-]?key|token|secret|password" echo-dots-2-hermes/ \
  --include="*.conf" --include="*.service" --include="*.sh" --include="*.py" \
  | grep -v "os.environ\|getenv\|#"
```

OpenAI 的 key **不在**這包裡——它由 Hermes 在 server 端解析，
從來沒有落地到任何檔案。
