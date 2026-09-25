"""config：缺值報錯、.env 白名單、Windows / Linux 字型與 Chrome 偵測、claude.cmd → claude.exe。"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from skillvideo import config
from skillvideo.config import ConfigError, ExternalCommandError


def test_missing_required_values_raise(tmp_path):
    env = {"KINDLE_REPO": str(tmp_path)}
    with pytest.raises(ConfigError) as info:
        config.load_config(dry_run=False, environ=env, detect_tools=False)
    for key in ("YT_TOKEN_PATH", "TELEGRAM_BOT_TOKEN", "GMAIL_APP_PASSWORD", "NOTIFY_EMAIL"):
        assert key in str(info.value)


def test_missing_kindle_repo_raises_even_in_dry_run():
    with pytest.raises(ConfigError, match="KINDLE_REPO"):
        config.load_config(dry_run=True, environ={}, detect_tools=False)


def test_blank_value_counts_as_missing(tmp_path):
    with pytest.raises(ConfigError, match="KINDLE_REPO"):
        config.load_config(dry_run=True, environ={"KINDLE_REPO": "   "}, detect_tools=False)


def test_dry_run_only_needs_kindle_and_uses_defaults(tmp_path):
    cfg = config.load_config(dry_run=True, environ={"KINDLE_REPO": str(tmp_path)}, detect_tools=False)
    assert cfg.kindle_repo == tmp_path
    assert cfg.work_dir == Path.home() / "skillvideo-work"
    assert cfg.tts_voice == "zh-TW-HsiaoChenNeural"
    assert cfg.tts_rate == "+8%"


def test_kindle_repo_must_be_directory(tmp_path):
    with pytest.raises(ConfigError, match="不是有效目錄"):
        config.load_config(dry_run=True, environ={"KINDLE_REPO": str(tmp_path / "nope")}, detect_tools=False)


def test_dotenv_loads_only_whitelisted_keys(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("# 註解\nKINDLE_REPO=/x\nUNRELATED_SECRET=abc\nTTS_RATE=\"+5%\"\n", encoding="utf-8")
    env: dict[str, str] = {"TTS_RATE": "+1%"}
    loaded = config.load_dotenv_file(dotenv, env)
    assert loaded == 1
    assert env["KINDLE_REPO"] == "/x"
    assert env["TTS_RATE"] == "+1%"          # 既有值不覆蓋
    assert "UNRELATED_SECRET" not in env


def test_detect_font_windows_branch():
    env = {"WINDIR": "C:\\Windows"}
    family, path, index = config.detect_font(env, system="Windows", exists=lambda p: p.name == "msjhbd.ttc")
    assert family == "Microsoft JhengHei"
    assert path.name == "msjhbd.ttc"
    assert index == 0


def test_detect_font_linux_branch():
    target = "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc"
    family, path, index = config.detect_font({}, system="Linux", exists=lambda p: p.as_posix() == target)
    assert family == "Noto Sans CJK TC"
    assert path.as_posix() == target
    assert index == config.LINUX_FONT_INDEX


def test_detect_font_missing_raises():
    with pytest.raises(ConfigError, match="Noto"):
        config.detect_font({}, system="Linux", exists=lambda p: False)
    with pytest.raises(ConfigError, match="正黑體"):
        config.detect_font({"WINDIR": "C:\\Windows"}, system="Windows", exists=lambda p: False)


def test_detect_chrome_windows_branch():
    env = {"PROGRAMFILES": "C:\\Program Files"}
    found = config.detect_chrome(env, system="Windows", which=lambda n: None,
                                 exists=lambda p: p.name == "chrome.exe")
    assert found.endswith("chrome.exe")
    assert "Google" in found


def test_detect_chrome_linux_branch():
    found = config.detect_chrome({}, system="Linux",
                                 which=lambda n: "/usr/bin/chromium-browser" if n == "chromium-browser" else None)
    assert found == "/usr/bin/chromium-browser"


def test_detect_chrome_override_and_missing():
    env = {"CHROME_BIN": "/opt/chrome"}
    assert config.detect_chrome(env, system="Linux", exists=lambda p: True) == "/opt/chrome"
    with pytest.raises(ConfigError):
        config.detect_chrome({}, system="Linux", which=lambda n: None)


def test_claude_cmd_resolves_to_node_modules_exe():
    cmd = "D:\\npm\\claude.cmd"
    which = {"claude.exe": None, "claude.cmd": cmd}.get
    found = config.resolve_claude_bin({}, system="Windows", which=which, exists=lambda p: True)
    path = Path(found)
    assert path.name == "claude.exe"
    assert path.parts[-4:] == ("@anthropic-ai", "claude-code", "bin", "claude.exe")
    assert "node_modules" in path.parts


def test_claude_prefers_exe_on_windows():
    which = {"claude.exe": "D:\\bin\\claude.exe", "claude.cmd": "D:\\npm\\claude.cmd"}.get
    assert config.resolve_claude_bin({}, system="Windows", which=which) == "D:\\bin\\claude.exe"


def test_claude_env_override_cmd_missing_exe_raises():
    with pytest.raises(ConfigError, match="claude.exe"):
        config.resolve_claude_bin({"CLAUDE_BIN": "D:\\npm\\claude.cmd"}, system="Windows",
                                  exists=lambda p: False)


def test_claude_linux_uses_which():
    assert config.resolve_claude_bin({}, system="Linux", which=lambda n: "/usr/bin/claude") == "/usr/bin/claude"
    with pytest.raises(ConfigError):
        config.resolve_claude_bin({}, system="Linux", which=lambda n: None)


class FakePopen:
    """記錄參數的假 Popen；timeout_first=True 時第一次 communicate 逾時。"""

    instances: list["FakePopen"] = []

    def __init__(self, args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.pid = 4321
        self.returncode = kwargs.pop("_rc", 0)
        self.stdout_text, self.stderr_text = "ok", ""
        self.timeout_first = False
        self.calls = 0
        FakePopen.instances.append(self)

    def communicate(self, input=None, timeout=None):
        self.calls += 1
        if self.timeout_first and self.calls == 1:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.stdout_text, self.stderr_text

    def poll(self):
        return self.returncode

    def kill(self):
        pass


def test_run_external_passes_utf8_env_and_no_shell(monkeypatch):
    FakePopen.instances.clear()
    monkeypatch.setattr(config.subprocess, "Popen", FakePopen)
    proc = config.run_external(["ffmpeg", "-version"], timeout=5, input_text="x", env={"A": "1"})
    kwargs = FakePopen.instances[0].kwargs
    assert proc.stdout == "ok" and proc.returncode == 0
    assert kwargs["encoding"] == "utf-8" and kwargs["shell"] is False
    assert kwargs["env"] == {"A": "1"}
    assert kwargs["start_new_session"] == (os.name == "posix")


def test_run_external_timeout_kills_process_tree(monkeypatch):
    killed = []

    def factory(args, **kwargs):
        proc = FakePopen(args, **kwargs)
        proc.timeout_first = True
        return proc

    monkeypatch.setattr(config.subprocess, "Popen", factory)
    monkeypatch.setattr(config, "_kill_process_tree", lambda proc: killed.append(proc.pid))
    with pytest.raises(ExternalCommandError, match="逾時"):
        config.run_external(["chrome"], timeout=1)
    assert killed == [4321]


def test_run_external_nonzero_includes_stderr(monkeypatch):
    def factory(args, **kwargs):
        proc = FakePopen(args, **kwargs)
        proc.returncode = 1
        proc.stdout_text, proc.stderr_text = "", "壞掉了\n細節"
        return proc

    monkeypatch.setattr(config.subprocess, "Popen", factory)
    with pytest.raises(ExternalCommandError, match="細節"):
        config.run_external(["ffmpeg"], timeout=1)


def test_filtered_child_env_strips_secrets():
    env = {"PATH": "/bin", "HOME": "/h", "ANTHROPIC_API_KEY": "x", "ANTHROPIC_AUTH_TOKEN": "y",
           "ANTHROPIC_MODEL": "m", "GMAIL_APP_PASSWORD": "p", "TELEGRAM_BOT_TOKEN": "t",
           "YT_TOKEN_PATH": "/t", "KINDLE_REPO": "/k"}
    child = config.filtered_child_env(env)
    assert child == {"PATH": "/bin", "HOME": "/h", "ANTHROPIC_MODEL": "m", "KINDLE_REPO": "/k"}
