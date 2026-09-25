"""qa：影片規格、敏感字串、敏感集數數字、標題重複、完整 run_qa（ffprobe / ffmpeg 以 mock 取代）。"""
from __future__ import annotations

from pathlib import Path

from conftest import make_script
from skillvideo import qa
from skillvideo.render import RenderResult

SESSION_REPORT_DEMO = """## 過程
- 共 12 次工具呼叫：`Skill`×1、`Bash`×7
_結束：turns=14，耗時 112s_
- `report.html`（約 369 KB）
報告涵蓋最近 7 天、300 多個 session 的 token 用量。
"""


def test_video_meta_pass_and_fail():
    ok = {"duration": 300.0, "width": 1920, "height": 1080, "has_audio": True}
    assert qa.check_video_meta(ok) == []
    bad = {"duration": 250.0, "width": 1280, "height": 720, "has_audio": False}
    errors = qa.check_video_meta(bad)
    assert len(errors) == 3


def test_sensitive_string_detection():
    samples = [
        "路徑 C:\\Users\\someone\\x", "在 /home/claude/x", "Git Bash 的 /c/Users/abc",
        "寄到 someone@example.com", "token ghp_abcdefghijklmnop", "金鑰 sk-ant-abcdef123456",
        "AIzaSyA1234567890abcdef",
        "macOS 的 /Users/alice/proj", "root 的 /root/.config", "設定在 ~/.claude/settings.json",
        "bot 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ", "slack xoxb-1234-abcdef",
    ]
    for text in samples:
        assert qa.find_sensitive_strings(text), text
    assert qa.find_sensitive_strings("用 /paperjsx 觸發，輸出 q3.pptx，這是 task-runner") == []


def test_sensitive_episode_number_leak_detection():
    facts = qa.extract_demo_facts(SESSION_REPORT_DEMO)
    assert {"12", "14", "112", "369", "300", "7"} <= facts
    assert "1" not in facts                      # 單一位數無單位不算
    leaked = qa.find_leaked_facts("這份報告統計了 300 多個 session", [SESSION_REPORT_DEMO])
    assert leaked == ["300"]
    assert qa.find_leaked_facts("它會統計各專案的用量佔比", [SESSION_REPORT_DEMO]) == []


def test_title_duplicate_ignores_spacing_and_punctuation():
    assert qa.is_duplicate_title("paperjsx：一句話做簡報", ["paperjsx : 一句話做簡報"])
    assert not qa.is_duplicate_title("paperjsx：新重點", ["paperjsx：一句話做簡報"])


def test_check_texts_combines_rules():
    script = make_script(title="重複標題")
    script["scenes"][1]["n"][0] = "這次跑了 112 秒"
    errors = qa.check_texts(script, "系列 #058｜重複標題", ["重複標題"], True, [SESSION_REPORT_DEMO])
    assert any("112" in e for e in errors)
    assert any("重複" in e for e in errors)
    assert qa.check_texts(make_script(title="新標題"), "x", [], False, []) == []


def test_run_qa_pass_takes_screenshots(tmp_path, monkeypatch):
    monkeypatch.setattr(qa, "probe_video",
                        lambda p: {"duration": 300.0, "width": 1920, "height": 1080, "has_audio": True})
    shots = []
    monkeypatch.setattr(qa, "extract_screenshots",
                        lambda video, dur, out: shots.extend([out / "a.jpg"]) or [out / "a.jpg"])
    render = RenderResult(Path("v.mp4"), Path("v.srt"), Path("t.jpg"), 300.0, "+8%")
    result = qa.run_qa(render, make_script(), "標題", [], False, [], tmp_path)
    assert result.passed and result.screenshots == [tmp_path / "a.jpg"]
    assert "QA 通過" in result.summary()


def test_run_qa_fail_skips_screenshots(tmp_path, monkeypatch):
    monkeypatch.setattr(qa, "probe_video",
                        lambda p: {"duration": 200.0, "width": 1920, "height": 1080, "has_audio": True})

    def must_not_call(*a, **k):
        raise AssertionError("QA 失敗時不應截圖")

    monkeypatch.setattr(qa, "extract_screenshots", must_not_call)
    script = make_script()
    script["scenes"][1]["code"] = "\n".join(["x"] * 12)
    render = RenderResult(Path("v.mp4"), Path("v.srt"), Path("t.jpg"), 200.0, "+8%")
    result = qa.run_qa(render, script, "標題 /home/x", [], False, [], tmp_path)
    assert not result.passed
    joined = "\n".join(result.errors)
    assert "長度" in joined and "溢出" in joined and "敏感字串" in joined
