# Claude Code Skill 技術探討（每日自動影片）

每天 20:00（台北）自動製作**隔天兩集**、各約 5 分鐘的繁體中文教學影片，上傳到 YouTube 並預約
**02:00、14:00 公開**，再用 Gmail + Telegram 通知。主題依序取自 `topics.json`（73 集），
每集講 1～2 個 Claude Code Skill 的實測結果（素材來自 kindle-98 repo 的 Demo 紀錄）。

完整規格見 [SPEC.md](SPEC.md)。

## 流程

```text
topics.json → state.json 選集數
  → script_gen：Demo 紀錄 + SKILL.md → claude -p（無工具、1 回合）→ scenes.json（schema 驗證）
  → render：投影片 PNG（Chrome）→ edge-tts 語音 → 字幕畫面 → ffmpeg 合成 → SRT、縮圖
  → qa：長度 / 解析度 / 溢出 / 敏感字串 / 標題重複 → 截圖
  → youtube：（必要時先反查既有影片）→ 私人 + publishAt 上傳 → 立刻記 uploaded
            → 字幕 / 縮圖 / 播放清單（失敗只警告）→ scheduled
  → notify：Gmail + Telegram
```

## 安裝

需要：Python 3.11+、ffmpeg / ffprobe、Chrome 或 Chromium、繁中字型、已登入的 claude CLI。

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt          # Windows：.venv/Scripts/pip
```

Ubuntu（VPS）：

```bash
sudo apt install -y python3-venv ffmpeg fonts-noto-cjk
# Ubuntu 的 chromium 套件是 snap 版，headless 截圖常因沙箱 / 路徑權限失敗，建議改裝 Google Chrome 官方 deb：
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y ./google-chrome-stable_current_amd64.deb
# 若 Chrome 裝在非標準位置，於 .env 設定 CHROME_BIN
```

字型：Windows 用微軟正黑體（`msjhbd.ttc`），Linux 用 Noto Sans CJK TC，會自動偵測，找不到就報錯。

## 設定

```bash
cp .env.example .env    # 填入 KINDLE_REPO、YT_TOKEN_PATH、Telegram、Gmail 等
chmod 600 .env
```

- `.env` 由程式以**白名單**載入（只認 `.env.example` 列出的鍵）；systemd 不用 `EnvironmentFile`，
  呼叫 claude CLI 時也會剝除 Gmail / Telegram / YouTube 憑證與 Anthropic 金鑰類變數。
- **不要**設定 Anthropic API 金鑰：腳本走 `claude -p`（Max 訂閱額度）。
- YouTube token 需 scope `youtube.force-ssl` 且含 `refresh_token`，檔案權限 600。
- `state.json` 首次執行時由 `topics.json` 自動建立（每次存檔前保留 `state.json.bak`），不進版控。

## 執行

```bash
export PYTHONUTF8=1
python -m skillvideo.run_daily --dry-run            # 做到 QA 為止，不上傳、不通知、不寫 state
python -m skillvideo.run_daily --dry-run --eps 1    # 只試做第 1 集
python -m skillvideo.run_daily                      # 正式：製作隔天兩集並預約公開
python -m skillvideo.run_daily --date 2026-09-26    # 指定製作日（slot = 隔天 02:00、14:00）
python -m skillvideo.run_daily --eps 5 --force      # 重做已排程的第 5 集（沒有 --force 會拒絕）
python -m skillvideo.run_daily run --catch-up       # 只補今天尚未過、尚未排程的 slot
python -m skillvideo.run_daily verify               # 檢查已到公開時間的集數是否真的 public
python -m pytest -q                                 # 測試（外部服務全部 mock）
```

結束碼：0 成功、1 有集數失敗、2 設定 / 參數錯誤、3 另一個程序執行中。
產物與 log 在 `WORK_DIR`（預設 `~/skillvideo-work`）的 `episodes/`、`logs/YYYY-MM-DD.log`，保留 7 天；
上傳成功後會刪除中間產物，只留 mp4、srt、縮圖與 QA 截圖。

## 失敗處理

- **不重複上傳**：拿到 video_id 立刻寫 state；補跑時已有影片的 slot 直接跳過；曾失敗 / state 遺失時先用
  描述尾端的 `[skillvideo:epNNN]` 標記到自己頻道反查，找到就沿用。上傳後若 state 寫不進去，會中止並通知 video_id。
  **不要手動刪除影片描述尾端的 `[skillvideo:epNNN]` 標記**，防重複上傳依賴它。
- `--force` 只能把該集排回它原本的 slot；重傳前會通知舊影片連結，請手動刪除或改為私人。
- 每集失敗累計 `attempts`，達 3 次暫停該集並通知，之後自動改排下一集；73 集做完只通知一次「系列完結」。
- 距公開不到 60 分鐘不再開始製作；上傳前距公開不到 15 分鐘放棄上傳；不到 2 小時的失敗標示「此 slot 將開天窗」。
- YouTube 配額用盡 / 需要重新授權 → 立即停止上傳並通知，不累計失敗次數；正式執行一開始就先驗證 YouTube 認證。
- 設定載不起來、未預期例外、verify 查詢失敗都會通知；執行鎖被占用超過 30 分鐘也會通知。

## 部署（VPS，systemd user units）

user unit 需要 **linger**，否則使用者登出後 timer 不會觸發：

```bash
mkdir -p ~/.config/systemd/user
cp deploy/*.service deploy/*.timer ~/.config/systemd/user/
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now skillvideo-run.timer skillvideo-verify.timer skillvideo-catchup.timer
systemctl --user list-timers | grep skillvideo
```

- `skillvideo-run.timer`：每天 20:00、22:00、00:00（Asia/Taipei）。
- `skillvideo-catchup.timer`：每天 08:00，`run --catch-up` 補救當天 14:00。
- `skillvideo-verify.timer`：每天 02:30、14:30。
- unit 預設 repo 位於 `~/claude-code-skill-video`、venv 位於其下 `.venv/`；
  user unit 無法可靠等待 `network-online.target`，網路未就緒由程式內重試處理。
