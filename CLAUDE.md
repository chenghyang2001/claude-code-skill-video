# CLAUDE.md — claude-code-skill-video

## 專案概述

「Claude Code Skill 技術探討」每日自動影片流水線：讀 kindle-98 repo 的 Demo 紀錄 → `claude -p` 產腳本 →
投影片 + edge-tts + ffmpeg 合成 5 分鐘影片 → QA → YouTube 預約公開（02:00 / 14:00 台北）→ Gmail + Telegram 通知。
規格唯一依據是 `SPEC.md`。開發在 Windows（Git Bash），正式在 VPS（Ubuntu，systemd user timer），**兩邊都要能跑**。

## 架構

```text
skillvideo/
├── config.py      環境變數（.env 白名單載入）、平台偵測（字型 / Chrome / claude.exe）、run_external（逾時砍程序樹）、子程序機密過濾
├── state.py       state.json 原子寫入 + .bak、狀態流轉（含 uploaded）、OS 級 FileLock（flock / msvcrt）、鎖忙碌追蹤
├── script_gen.py  素材收集（Demo 紀錄 + SKILL.md）、prompt、claude CLI 呼叫與 JSON 解析、schema 驗證
├── render.py      投影片 HTML/PNG、字級與溢出規則、TTS + 語速校正、字幕畫面、SRT、ffmpeg、縮圖
├── qa.py          品質閘門（長度 / 解析度 / 音軌 / 溢出 / 敏感字串 / 敏感集數數字 / 標題重複）+ 截圖
├── youtube.py     body 組裝（唯一標記）、token refresh、錯誤分類與重試、反查既有影片、上傳與上傳後步驟（降級警告）
├── notify.py      Telegram、Gmail、訊息格式
└── run_daily.py   CLI（run / verify）、slot 計算、單集流程、失敗處理
tests/             pytest，所有外部服務 mock
deploy/            systemd user service + timer（run 20/22/00 時、catchup 08 時、verify 02:30/14:30）
topics.json        73 集主題（唯讀）；templates/slide.css 投影片樣式（唯讀）
```

## 重要決策

- **時區**：用固定 `+08:00`（`config.TAIPEI_TZ`），不用 zoneinfo，因 Windows 可能缺 tzdata；台灣無日光節約。
- **製作日**：06:00 前算前一天（`ROLLOVER_HOUR`），所以 00:00 補跑仍指向同一批 slot。
- **冪等**：slot 以 `publish_at`（RFC3339 UTC）識別；state 中已有 scheduled / published 就跳過。
- **claude CLI**：prompt 走 stdin（避開 Windows 32K 命令列上限）；Windows npm 版 `claude.cmd` 解析成
  `node_modules/@anthropic-ai/claude-code/bin/claude.exe`；不使用 `--bare`（會改走 API 計費）。
- **禁止**讀寫 Anthropic API 金鑰環境變數；`.env` 只載入 `KNOWN_ENV_KEYS` 白名單。
- **Chrome** 加獨立 `--user-data-dir`，避免與使用者開著的 Chrome 搶設定檔導致截圖失敗。
- **程式碼框**：字級依 SPEC（≤8 行 40px、≤10 行 34px、其餘 30px），9 行以上行高改 1.35 才放得下；
  寬度超出再縮字級；QA 用同一套規則近似判斷溢出。
- **配額 / 授權錯誤**不累計 attempts，且立即停止後續上傳；播放清單失敗不影響已排程的影片。
- **防重複上傳三道防線**：①videos.insert 拿到 video_id 立刻寫 `uploaded` 並存檔；②上傳後步驟全部降級為警告，
  上傳後存檔失敗則中止整個 run 並通知 video_id；③曾渲染 / 上傳 / 失敗過或 state.json 遺失時，上傳前以描述尾端
  `[skillvideo:epNNN]`（另有 tag `skillvideo-epNNN`）到 uploads 清單反查並沿用。
- **鎖**：OS 級鎖（行程死亡自動釋放），忙碌超過 30 分鐘通知一次（`run.lock.busy` 紀錄）。
- **機密**：systemd 不用 EnvironmentFile；claude 子程序環境剝除 GMAIL_* / TELEGRAM_* / YT_* 與 Anthropic 金鑰類變數。
- **claude 參數**：`--setting-sources project,local --strict-mcp-config --max-turns 1 --tools ""`，prompt 宣告素材是資料不是指令。
- **時間門檻**：開始製作需距公開 ≥ 60 分鐘；上傳前再檢查 ≥ 15 分鐘。

## 執行

```bash
PYTHONUTF8=1 python -m skillvideo.run_daily --dry-run --eps 1
PYTHONUTF8=1 python -m pytest -q
```

## 修改守則

- 程式碼檔（.py / .yml 等）走 code-writer → code-qa → code-reviewer 流程。
- 路徑一律 `Path.home()` / 環境變數，不寫死使用者名稱；subprocess 一律經 `config.run_external`（UTF-8 + timeout + 不經 shell）。
- 不要修改 `SPEC.md`、`topics.json`、`templates/slide.css`。
