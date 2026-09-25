# 專案報告：Claude Code Skill 技術探討（每日自動影片）

> 日期：2026-09-26　狀態：**已部署 VPS，等待首播**
> Repo：<https://github.com/chenghyang2001/claude-code-skill-video>

## 一、這是什麼

每天在 VPS 上自動製作 2 集、各約 5 分鐘的繁體中文教學影片，主題是《10 倍速！AI Skill 全攻略》書中範例 Skill 的**實測結果**，
以預約方式公開到個人 YouTube 頻道「ChengHsien Yang」，並用 Gmail + Telegram 通知。

## 二、使用者決定

| 項目 | 決定 |
| --- | --- |
| 執行位置 | VPS（claude@187.127.109.145） |
| 發布 | 每天 2 集，**02:00、14:00**（台北）自動公開；前一晚 20:00 製作 |
| 系列名稱 | Claude Code Skill 技術探討（標題 `#001｜<Skill>：<重點>`） |
| 主題 | Kindle 98 已實測的 76 個 Demo，3 組內容太薄的合併 → **73 集（約 36 天）** |
| 公開方式 | 直接公開，不做樣片審核 |
| 通知 | Gmail + Telegram |

## 三、時程

| 時間（台北） | 事件 |
| --- | --- |
| 2026-09-25 | p169 範例重現：書附沒有 `ai-teaching-video` Skill，改用免費工具鏈做出 5 分鐘影片（<https://youtu.be/vFsTGij5XGs> ，不公開） |
| 2026-09-26 | 規格書、73 集主題清單、程式開發（三輪審查通過）、本機與 VPS 乾跑、部署上線 |
| **2026-09-26 20:00** | 第一次正式製作 #001 paperjsx、#002 frontend-design |
| **2026-09-27 02:00 / 14:00** | #001、#002 首播 |

## 四、流水線

```text
topics.json（下一集）→ claude -p 寫腳本（讀實測紀錄 + SKILL.md）→ 投影片（HTML→Chrome）
→ edge-tts 語音 → 字幕燒入 + SRT → ffmpeg 合成 → 品質閘門 → YouTube 預約公開 → 字幕/縮圖/播放清單 → 通知
```

每集固定結構：封面 → 這個 Skill 做什麼 → 怎麼觸發 → 實測示範 → 踩到的坑 → 適合誰用與總結。

## 五、品質與安全

- **開發流程**：code-writer → code-qa（5 層驗證）→ code-reviewer（三輪，最終 APPROVED），**138 個 pytest** 全數通過。
- **品質閘門**（不過就不上傳）：長度 270～330 秒、1920×1080、有音軌、程式碼框不溢出、無本機路徑／token／email、敏感集數不引用數據、標題不重複。
- **防重複上傳三道防線**：開始上傳前存 `rendered` → 拿到 video_id 立即存 `uploaded` → 上傳前以描述尾端 `[skillvideo:epNNN]` 標記到 YouTube 反查。
- **防開天窗**：20:00 製作、22:00／00:00 補跑、08:00 補做當天未排的 slot；OS 級檔案鎖（行程死亡自動釋放）。
- **不會靜默失敗**：設定錯誤、任何未預期例外、鎖忙碌超過 30 分鐘、公開後被鎖成私人 → 都會通知。
- **機密**：claude 子程序剝除 Gmail／Telegram／YouTube／Anthropic 金鑰變數；呼叫參數 `--setting-sources project,local --strict-mcp-config --max-turns 1 --tools ""`，素材只當資料不當指令；走 Max 訂閱，不用 API Key。

## 六、驗證結果

| 驗證 | 結果 |
| --- | --- |
| 本機乾跑 | #001 297.1 秒、#002 292.2 秒，QA 通過；腳本內容與實測紀錄一致 |
| VPS 乾跑 | #001 299.0 秒、#002 286.3 秒，QA 通過；Noto 繁中字型正常 |
| VPS 測試 | 138 passed |
| 通知測試 | Telegram、Gmail 皆成功 |
| OAuth | 專案 986815740713 為正式版（token 37 天仍可刷新），scope `youtube.force-ssl` 足夠 |

## 七、VPS 部署

| 項目 | 內容 |
| --- | --- |
| 環境 | Ubuntu 24.04、systemd 255、Python 3.12、Chrome 154（deb）、claude 2.1.282（Max 訂閱） |
| 路徑 | 程式 `~/claude-code-skill-video`（`.env` 權限 600）、內容 `~/kindle-98-ai-skill-10x-guide`、token `~/.config/skillvideo/youtube-token.json`（600） |
| 排程（systemd user timer） | run 20:00／22:00／00:00、verify 02:30／14:30、catchup 08:00（9/26 09:00 由一次性 timer 啟用） |
| 產物 | `~/skillvideo-work`，保留 7 天，上傳後刪中間檔 |

## 八、注意事項與已知限制

1. catchup timer 由一次性 timer 啟用，若 VPS 在 9/26 09:00 前重開機會失效 → 確認 `systemctl --user is-enabled skillvideo-catchup.timer`。
2. 本機與 VPS 共用同一份 YouTube token；本機若重新授權，要同步更新 VPS。
3. VPS 磁碟已用 82%。
4. 防重複上傳依賴影片描述尾端的 `[skillvideo:epNNN]` 標記，**不要手動刪除**。
5. 沿用既有影片時字幕可能再傳一次（最壞多一條相同字幕軌）；中英混排字幕換行有時不理想。
6. 73 集約 36 天播完，之後主題待決定（例如剩下 50 個 Demo 補測後接上）。

## 九、常用指令（VPS）

```bash
systemctl --user list-timers                       # 看下次觸發時間
journalctl --user -u skillvideo-run -n 50          # 看最近一次製作 log
cd ~/claude-code-skill-video && .venv/bin/python -m skillvideo.run_daily --dry-run   # 乾跑
.venv/bin/python -m skillvideo.run_daily verify    # 手動檢查公開狀態
```

## 十、相關文件

- 規格書：`SPEC.md`；使用說明：`README.md`
- p169 重現紀錄與 Kindle 98 Demo 總表：<https://github.com/chenghyang2001/kindle-98-ai-skill-10x-guide> （`doc/demos/`）
