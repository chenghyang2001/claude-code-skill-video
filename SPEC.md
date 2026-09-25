# 規格書：Claude Code Skill 技術探討（每日自動影片）

> 版本 1.0（2026-09-26）。以 kindle-98 repo 的 p169 Demo（<https://youtu.be/vFsTGij5XGs）為範本。>
> 最終在 VPS（Ubuntu，Hostinger）排程執行；開發在 Windows（Git Bash），**程式必須兩邊都能跑**。

## 1. 目標

每天 20:00（Asia/Taipei）自動做好**隔天兩集**、各約 5 分鐘的繁體中文教學影片，
上傳到個人 YouTube 頻道「ChengHsien Yang」，以 `publishAt` 預約 **02:00、14:00 公開**，並用 Gmail + Telegram 通知。
主題依序取自 `topics.json`（73 集，每集講 1～2 個 Skill 的實測結果）。

## 2. 使用者已確認的決定

| 項目 | 決定 |
| --- | --- |
| 系列名稱 | Claude Code Skill 技術探討 |
| 標題格式 | `Claude Code Skill 技術探討 #001｜<skill 名>：<一句重點>`（≤ 100 字元） |
| 發布 | 每天 2 集，預約 02:00、14:00（台北）公開；前一晚 20:00 製作，22:00、00:00 補跑 |
| 公開方式 | 直接公開（`publishAt` 到點自動公開），不需人工審核 |
| 通知 | Gmail + Telegram：製作成功（附連結與預定公開時間）、失敗（附錯誤摘要）、公開後驗證結果 |

## 3. 目錄結構

```text
claude-code-skill-video/
├── SPEC.md / README.md / CLAUDE.md / .gitignore / .env.example
├── topics.json            # 73 集主題（已產生，見 §5）
├── state.json             # 執行狀態（不進版控；首次執行自動由 topics.json 初始化）
├── templates/slide.css    # 投影片樣式（沿用 p169）
├── skillvideo/             # Python 套件
│   ├── config.py          # 讀環境變數、平台差異（字型、Chrome 路徑）、常數
│   ├── script_gen.py      # 讀 Demo 紀錄 → 呼叫 `claude -p` → scenes.json（含 schema 驗證）
│   ├── render.py          # 投影片 PNG、TTS、字幕畫面、ffmpeg 合成、縮圖、SRT
│   ├── qa.py              # 品質閘門
│   ├── youtube.py         # 上傳（預約公開）、字幕、播放清單、公開後驗證
│   ├── notify.py          # Gmail（SMTP）+ Telegram（Bot API）
│   ├── state.py           # 狀態檔讀寫（原子寫入 + 檔案鎖）
│   └── run_daily.py       # 主流程（CLI 入口）
├── deploy/                # systemd service + timer（20:00 / 22:00 / 00:00）
└── tests/                 # pytest
```

## 4. 設定（環境變數，`.env.example` 要列齊，缺必要值啟動即報錯）

| 變數 | 用途 | 必要 |
| --- | --- | --- |
| `KINDLE_REPO` | kindle-98 repo 路徑（讀 `doc/demos/*.md` 與 `code/`） | ✅ |
| `YT_TOKEN_PATH` | YouTube OAuth token（scope `youtube.force-ssl`，需含 refresh_token） | ✅（dry-run 除外） |
| `YT_PLAYLIST_ID` | 系列播放清單；未設定則首次自動建立並寫回 state.json | |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram 通知 | ✅（dry-run 除外） |
| `GMAIL_USER` / `GMAIL_APP_PASSWORD` / `NOTIFY_EMAIL` | Gmail SMTP 通知 | ✅（dry-run 除外） |
| `WORK_DIR` | 產物目錄（預設 `~/skillvideo-work`），保留 7 天 | |
| `CLAUDE_BIN` | claude CLI 路徑（預設自動偵測；Windows npm 版要解析到 `claude.exe`，不可直接呼叫 `claude.cmd`） | |
| `CHROME_BIN` | Chrome / Chromium 路徑（預設依平台偵測） | |
| `TTS_VOICE` / `TTS_RATE` | 預設 `zh-TW-HsiaoChenNeural` / `+8%` | |

**禁止**設定或讀取 `ANTHROPIC_API_KEY`（腳本走 `claude -p` 的 Max 訂閱額度）。
路徑一律 `Path.home()` / 環境變數，不寫死使用者名稱。

## 5. topics.json（已存在，唯讀）

```json
[{"ep": 1, "state": "pending",
  "skills": [{"id": "09-11", "name": "paperjsx", "status": "✅", "demo_md": "doc/demos/09-11-paperjsx.md", "sensitive": false}]}]
```

- `demo_md` 相對於 `KINDLE_REPO`；`status` 🟡 代表有條件能跑（腳本要講清楚條件）。
- `sensitive: true`（10-21 session-report）：**腳本不得引用 Demo 紀錄中的任何數據**，只講用法與注意事項。
- 執行進度存在 `state.json`：`{"episodes": {"1": {"state": "scheduled", "video_id": "...", "publish_at": "...", "attempts": 0, "last_error": null}}, "playlist_id": "..."}`；
  state 流轉：`pending → rendered → scheduled → published`；失敗記 `failed` 並累計 `attempts`（≥ 3 次暫停該集，通知人工處理，改排下一集）。

## 6. 主流程 `python -m skillvideo.run_daily [--dry-run] [--date YYYY-MM-DD] [--eps N,N]`

1. 取得檔案鎖（避免 20:00 / 22:00 / 00:00 重疊執行）；
2. 計算目標 slot：「明天 02:00」「明天 14:00」（台北時間，轉成 RFC3339 UTC）；
3. 對每個 slot：若 state.json 已有該 slot 且 `scheduled` → 跳過（**冪等**，補跑不會重複上傳）；否則取下一個 `pending`（或 `failed` 且 attempts < 3）的集數；
4. 產生腳本 → 渲染 → QA → 上傳（`privacyStatus=private` + `publishAt`）→ 字幕 → 加入播放清單 → 更新 state；
5. 每集完成或失敗都通知；全部失敗且距離公開不到 2 小時要特別標示「此 slot 將開天窗」；
6. `--dry-run`：做到 QA 為止，不上傳、不通知（只印出），產物照樣留在 WORK_DIR。
7. 另一個子命令 `verify`：檢查已過 `publishAt` 的集數，確認 `privacyStatus == public`；若被鎖成私人（未驗證 API 專案限制）立即通知。排在 02:30、14:30 執行。

## 7. 腳本產生 `script_gen.py`

- 輸入：該集 1～2 個 Skill 的 Demo 紀錄全文 + 書附 SKILL.md（在 `KINDLE_REPO/code/<章節>/<dir>/**/SKILL.md` 找，找不到就略過）。
- 呼叫：`claude -p <prompt> --output-format json`（非互動，timeout 600 秒，失敗重試 1 次）。從回傳 JSON 的 `result` 取出腳本 JSON。
- prompt 要求固定 5 段結構：①這個 Skill 做什麼 ②怎麼觸發 ③實測示範（引用 Demo 真實輸入與結果）④踩到的坑／限制 ⑤適合誰用＋總結；
  前面加 1 張封面段（系列名、集數、Skill 名）。
- 產出 schema（缺欄位、型別錯、段數不在 6～9、句數總和不在 45～70 → 驗證失敗、重生一次）：

```json
{"title": "…（不含系列前綴，≤ 60 字）", "description": "…（2～4 句）", "tags": ["…"],
 "scenes": [{"t": "段落標題", "sub": "副標", "code": "投影片程式碼或重點（可為 null，≤ 10 行、每行 ≤ 48 字）", "n": ["旁白句 1", "…"]}]}
```

- 旁白：繁體中文、口語、每句 ≤ 40 字；不可出現本機路徑、帳號、token、email、私人資料；不可捏造 Demo 裡沒有的結果。

## 8. 渲染 `render.py`（沿用 p169 已驗證做法）

| 步驟 | 做法 |
| --- | --- |
| 投影片 | 每段一個 HTML（`templates/slide.css`），Chrome headless：`--headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1 --window-size=1920,1080 --screenshot=<png> file:///<html>`；程式碼框依行數縮字級（≤8 行 40px、≤10 行 34px、其餘 30px）；底部留 200px 給字幕；右上角顯示「#001」 |
| 字型 | Windows：`Microsoft JhengHei` / `msjhbd.ttc`；Linux：`Noto Sans CJK TC` / `NotoSansCJK-Bold.ttc`（由 config 偵測，找不到就報錯） |
| 語音 | edge-tts 逐句產生 mp3（失敗重試 3 次，間隔 2 秒；每句間隔 0.5 秒避免限流），ffprobe 量長度 |
| 長度校正 | 總長（每句 + 0.4 秒停頓）目標 285～315 秒；超出時調整語速（`-10%`～`+20%` 範圍內）重產一次；仍超出由 QA 判定 |
| 字幕 | Pillow 在每句畫面底部畫半透明黑底白字（50px，30 字換行），另輸出 SRT |
| 合成 | 每句「圖片 + 音訊」用 ffmpeg 編成片段（`-loop 1 -framerate 30`、libx264 `-tune stillimage` `yuv420p`、AAC 160k 48kHz 立體聲、`apad`+`-t`），再 `concat` 串接、`-movflags +faststart` |
| 縮圖 | 封面投影片 1280×720 JPG |

## 9. 品質閘門 `qa.py`（任一不過 → 該集 failed，不上傳）

1. 影片長度 270～330 秒、1920×1080、有音軌；
2. 每張投影片的程式碼框沒有溢出（渲染時 JS 回報 `scrollHeight <= clientHeight`，或以行數/字數規則近似判斷）；
3. 腳本與標題不含：`C:\Users`、`/home/`、`/c/Users`、email 格式、`gho_`/`ghp_`/`sk-`/`AIza` 等 token 樣式；
4. `sensitive` 集數額外檢查：不得出現 Demo 紀錄中的數字（token 數、session 數、專案名）；
5. 標題不與既往集數重複；
6. 抽 3 個時間點截圖存檔（10%、50%、90%），附在成功通知裡。

## 10. YouTube `youtube.py`

- 上傳：resumable（4MB chunk），`snippet`（系列標題、description = 腳本描述 + 固定頁尾〔系列介紹、「本影片由 AI 自動生成」聲明、kindle-98 repo 說明〕、tags、`categoryId=27`、`defaultLanguage=zh-Hant`）、
  `status`（`privacyStatus=private`、`publishAt`、`selfDeclaredMadeForKids=false`、`containsSyntheticMedia=true`）。
- 字幕：`captions.insert` 上傳 SRT（語言 zh-Hant）；失敗只記警告不擋上架。
- 縮圖：`thumbnails.set`；失敗只記警告。
- 播放清單：沒有就建立「Claude Code Skill 技術探討」（public），把影片加入。
- API 錯誤：quota（403 quotaExceeded）→ 不重試、通知；5xx → 指數退避重試 3 次。
- token 過期自動 refresh；refresh 失敗 → 通知「需要重新授權」。

## 11. 通知 `notify.py`

- Telegram：Bot API `sendMessage`（純文字），失敗記 log 不中斷。
- Gmail：`smtplib` SSL 465，`GMAIL_APP_PASSWORD`；HTML 內文附縮圖／截圖附件（每張 ≤ 300KB）。
- 內容：集數、標題、影片連結、預定公開時間（台北）、長度、QA 摘要；失敗則附錯誤最後 20 行。

## 12. 錯誤處理與日誌

- 每次執行寫 `WORK_DIR/logs/YYYY-MM-DD.log`（logging，UTF-8）；外部指令（claude、ffmpeg、chrome）都要 timeout 並擷取 stderr。
- 所有 subprocess 加 `encoding="utf-8"`；Windows 上不經 shell。
- 任何未預期例外 → 該集 failed + 通知，不讓整個程序靜默結束。

## 13. 部署（交給 @小雲，另開階段 C）

systemd timer（`OnCalendar=*-*-* 20:00,22:00,00:00 Asia/Taipei` 執行 run_daily；`02:30,14:30` 執行 verify）、
VPS 套件：python3-venv、ffmpeg、chromium、fonts-noto-cjk、edge-tts、google-api-python-client、google-auth-oauthlib；token 權限 600。

## 14. 測試（複雜等級，20+ 案例；外部服務一律 mock）

涵蓋：config 缺值報錯、slot 時間換算（跨日、UTC）、冪等跳過、failed 重試上限、state 原子寫入、schema 驗證（缺欄位／段數／句數）、
敏感字串偵測、sensitive 集數數字偵測、標題重複、程式碼字級規則、字幕換行、SRT 時間格式、長度校正語速計算、
YouTube 上傳 body 內容（publishAt 格式、containsSyntheticMedia）、quota 錯誤不重試、5xx 重試、通知失敗不中斷、dry-run 不呼叫上傳與通知、
Windows 與 Linux 字型／Chrome 偵測分支、`claude.cmd` → `claude.exe` 解析。
