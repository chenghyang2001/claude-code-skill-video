"""共用 fixture：假設定、假 topics、假通知器、合法腳本產生器。外部服務一律不真的呼叫。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skillvideo.config import CSS_PATH, Config  # noqa: E402


def make_topics(count: int = 4) -> list[dict[str, Any]]:
    return [{"ep": i, "state": "pending",
             "skills": [{"id": f"09-{i:02d}", "name": f"skill{i}", "status": "✅",
                         "demo_md": f"doc/demos/09-{i:02d}-skill{i}.md", "sensitive": False}]}
            for i in range(1, count + 1)]


def make_script(scene_count: int = 7, per_scene: int = 8, title: str = "paperjsx：一句話做出簡報") -> dict[str, Any]:
    scenes = []
    for idx in range(scene_count):
        scenes.append({"t": f"段落{idx}", "sub": "副標", "code": None if idx == 0 else "print('hi')",
                       "n": [f"這是第 {idx} 段的第 {j} 句旁白。" for j in range(per_scene)]})
    return {"title": title, "description": "介紹這個 Skill。實測結果整理。", "tags": ["Claude", "Skill"],
            "scenes": scenes}


class RecordingNotifier:
    """記錄 send 呼叫的假通知器。"""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str, list[Path]]] = []

    def send(self, subject: str, text: str, attachments=()) -> dict[str, bool]:
        self.messages.append((subject, text, list(attachments)))
        return {"telegram": True, "gmail": True}


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    kindle = tmp_path / "kindle"
    (kindle / "doc" / "demos").mkdir(parents=True)
    for topic in make_topics():
        (kindle / topic["skills"][0]["demo_md"]).write_text(
            f"# Demo {topic['ep']}\n## 輸入\n做一份簡報\n", encoding="utf-8")
    return Config(
        kindle_repo=kindle, work_dir=tmp_path / "work", yt_token_path=tmp_path / "token.json",
        yt_playlist_id=None, telegram_bot_token="t", telegram_chat_id="c", gmail_user="u@example.test",
        gmail_app_password="p", notify_email="n@example.test", tts_voice="zh-TW-HsiaoChenNeural",
        tts_rate="+8%", claude_bin="claude", chrome_bin="chrome", font_family="Test Font",
        font_path=tmp_path / "font.ttc", state_path=tmp_path / "state.json", css_path=CSS_PATH,
    )


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()
