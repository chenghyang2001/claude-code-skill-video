"""Claude Code Skill 技術探討：每日自動影片流水線。

模組分工：
- config：環境變數、平台偵測（字型 / Chrome / claude CLI）、外部指令執行
- script_gen：讀 Demo 紀錄 → `claude -p` → 腳本 JSON（含 schema 驗證）
- render：投影片、TTS、字幕、ffmpeg 合成、縮圖、SRT
- qa：品質閘門
- youtube：上傳（預約公開）、字幕、縮圖、播放清單、公開後驗證
- notify：Gmail + Telegram
- state：state.json 讀寫（原子寫入）與檔案鎖
- run_daily：CLI 入口（run / verify）
"""

__version__ = "1.0.0"
