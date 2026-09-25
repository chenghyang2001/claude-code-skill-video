"""notify：Telegram / Gmail 失敗不中斷、附件大小限制、dry-run 只印出、訊息格式。"""
from __future__ import annotations

import json
import smtplib
import urllib.error
from datetime import datetime

from skillvideo import notify
from skillvideo.config import TAIPEI_TZ


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_telegram_failure_retries_then_returns_false():
    calls, sleeps = [], []

    def broken_opener(request, timeout):
        calls.append(1)
        raise urllib.error.URLError("offline")

    assert notify.send_telegram("TOKEN", "1", "hi", opener=broken_opener, sleep=sleeps.append) is False
    assert len(calls) == notify.NETWORK_RETRIES and len(sleeps) == notify.NETWORK_RETRIES - 1


def test_telegram_recovers_after_network_blip():
    calls = []

    def flaky(request, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.URLError("network not ready")
        return FakeResponse({"ok": True})

    assert notify.send_telegram("T", "1", "x", opener=flaky, sleep=lambda s: None) is True


def test_telegram_success_and_api_error():
    seen = {}

    def opener(request, timeout):
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse({"ok": True})

    assert notify.send_telegram("TOKEN", "42", "你好", opener=opener) is True
    assert seen["body"]["chat_id"] == "42" and seen["body"]["text"] == "你好"
    assert notify.send_telegram("T", "1", "x", opener=lambda r, timeout: FakeResponse({"ok": False})) is False


class FakeSMTP:
    sent = []

    def __init__(self, host, port, context=None, timeout=None, fail=False):
        self.fail = fail

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        if self.fail:
            raise smtplib.SMTPAuthenticationError(535, b"bad")

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def test_gmail_auth_failure_is_not_retried():
    created = []

    def factory(*a, **k):
        created.append(1)
        return FakeSMTP(*a, fail=True, **k)

    assert notify.send_gmail("u@example.test", "pw", "n@example.test", "主旨", "內文",
                             smtp_factory=factory, sleep=lambda s: None) is False
    assert len(created) == 1


def test_gmail_connection_error_retries():
    created = []

    def factory(*a, **k):
        created.append(1)
        raise ConnectionRefusedError("down")

    assert notify.send_gmail("u@example.test", "pw", "n@example.test", "主旨", "內文",
                             smtp_factory=factory, sleep=lambda s: None) is False
    assert len(created) == notify.NETWORK_RETRIES


def test_emergency_notify_uses_environ_only(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send_telegram", lambda token, chat, text, **k: sent.append((token, chat)) or True)
    monkeypatch.setattr(notify, "send_gmail", lambda *a, **k: sent.append("gmail") or True)
    result = notify.emergency_notify("主旨", "內文", {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "9"})
    assert sent == [("tok", "9")]
    assert result == {"telegram": True, "gmail": False}


def test_gmail_attachments_skip_files_over_300kb(tmp_path):
    small, big = tmp_path / "small.jpg", tmp_path / "big.jpg"
    small.write_bytes(b"x" * 1000)
    big.write_bytes(b"x" * (300 * 1024 + 1))
    FakeSMTP.sent.clear()
    assert notify.send_gmail("u@example.test", "pw", "n@example.test", "主旨", "內文",
                             [small, big], smtp_factory=FakeSMTP) is True
    names = [part.get_filename() for part in FakeSMTP.sent[0].iter_attachments()]
    assert names == ["small.jpg"]


def test_notifier_continues_when_telegram_fails(cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "send_telegram", lambda *a, **k: calls.append("tg") or False)
    monkeypatch.setattr(notify, "send_gmail", lambda *a, **k: calls.append("gmail") or True)
    result = notify.Notifier(cfg).send("主旨", "內文")
    assert calls == ["tg", "gmail"]
    assert result == {"telegram": False, "gmail": True}


def test_notifier_dry_run_only_prints(cfg, monkeypatch, capsys):
    def must_not_call(*a, **k):
        raise AssertionError("dry-run 不應送出通知")

    monkeypatch.setattr(notify, "send_telegram", must_not_call)
    monkeypatch.setattr(notify, "send_gmail", must_not_call)
    notify.Notifier(cfg, dry_run=True).send("主旨", "內文")
    assert "dry-run" in capsys.readouterr().out


def test_failure_message_marks_blackout_and_pause():
    slot = datetime(2026, 9, 27, 2, 0, tzinfo=TAIPEI_TZ)
    subject, text = notify.format_failure(5, slot, "\n".join(f"line{i}" for i in range(30)), 3, True, True)
    assert "開天窗" in subject and "#005" in subject
    assert "暫停" in text and "line29" in text and "line9" not in text
    subject, _ = notify.format_failure(5, slot, "err", 1, False, False)
    assert "開天窗" not in subject


def test_success_message_marks_reused_video():
    slot = datetime(2026, 9, 27, 14, 0, tzinfo=TAIPEI_TZ)
    subject, text = notify.format_success(3, "標題", "https://youtu.be/X", slot, 300.0, "QA 通過", reused=True)
    assert "沿用既有影片" in subject and "未重新上傳" in text
    subject, _ = notify.format_success(3, "標題", "https://youtu.be/X", slot, 300.0, "QA 通過")
    assert "沿用" not in subject
