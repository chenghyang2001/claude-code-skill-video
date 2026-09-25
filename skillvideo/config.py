"""設定載入、平台差異偵測（字型、Chrome、claude CLI）與外部指令執行。

所有路徑一律由環境變數或 Path.home() 推導，不寫死任何使用者名稱。
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import signal
import subprocess
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, MutableMapping, Optional, Sequence

logger = logging.getLogger(__name__)

# ---- 常數 ----
SERIES_NAME = "Claude Code Skill 技術探討"
# 台灣沒有日光節約時間，用固定 +8 偏移；避免 Windows 缺 tzdata 時 zoneinfo 找不到 Asia/Taipei
TAIPEI_TZ = timezone(timedelta(hours=8), name="Asia/Taipei")
SLOT_HOURS = (2, 14)
# 00:00 補跑時「明天」仍應指向同一批 slot，所以 06:00 前算作前一天的製作日
ROLLOVER_HOUR = 6
MAX_ATTEMPTS = 3
WORK_RETENTION_DAYS = 7

REPO_ROOT = Path(__file__).resolve().parent.parent
TOPICS_PATH = REPO_ROOT / "topics.json"
STATE_PATH = REPO_ROOT / "state.json"
CSS_PATH = REPO_ROOT / "templates" / "slide.css"
DOTENV_PATH = REPO_ROOT / ".env"

DEFAULT_TTS_VOICE = "zh-TW-HsiaoChenNeural"
DEFAULT_TTS_RATE = "+8%"

# .env 只載入這些已知鍵，其他鍵（包含任何 API 金鑰）一律忽略，避免意外改變 claude CLI 的計費來源
KNOWN_ENV_KEYS = (
    "KINDLE_REPO", "YT_TOKEN_PATH", "YT_PLAYLIST_ID",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "GMAIL_USER", "GMAIL_APP_PASSWORD", "NOTIFY_EMAIL",
    "WORK_DIR", "CLAUDE_BIN", "CHROME_BIN", "TTS_VOICE", "TTS_RATE",
)
REQUIRED_ALWAYS = ("KINDLE_REPO",)
REQUIRED_UNLESS_DRY = (
    "YT_TOKEN_PATH", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "GMAIL_USER", "GMAIL_APP_PASSWORD", "NOTIFY_EMAIL",
)

WINDOWS_FONT_FAMILY = "Microsoft JhengHei"
WINDOWS_FONT_FILES = ("msjhbd.ttc", "msjh.ttc")
LINUX_FONT_FAMILY = "Noto Sans CJK TC"
LINUX_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)
# NotoSansCJK 的 ttc 內含 JP/KR/SC/TC/HK，索引 3 才是繁中字形
LINUX_FONT_INDEX = 3
LINUX_CHROME_NAMES = ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome")
CLAUDE_EXE_RELATIVE = Path("node_modules") / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"


class ConfigError(Exception):
    """設定缺漏或平台偵測失敗。"""


class ExternalCommandError(Exception):
    """外部指令（claude / ffmpeg / chrome）執行失敗。"""


@dataclass
class Config:
    """執行期設定（由環境變數組成）。"""

    kindle_repo: Optional[Path]
    work_dir: Path
    yt_token_path: Optional[Path]
    yt_playlist_id: Optional[str]
    telegram_bot_token: Optional[str]
    telegram_chat_id: Optional[str]
    gmail_user: Optional[str]
    gmail_app_password: Optional[str]
    notify_email: Optional[str]
    tts_voice: str
    tts_rate: str
    dry_run: bool = False
    claude_bin: Optional[str] = None
    chrome_bin: Optional[str] = None
    font_family: Optional[str] = None
    font_path: Optional[Path] = None
    font_index: int = 0
    state_path: Path = STATE_PATH
    topics_path: Path = TOPICS_PATH
    css_path: Path = CSS_PATH


# ---- .env 載入 ----
def load_dotenv_file(path: Path, environ: MutableMapping[str, str]) -> int:
    """讀取 .env（KEY=VALUE），只補上尚未設定的已知鍵；回傳載入數量。"""
    if not path.is_file():
        return 0
    loaded = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"無法讀取 {path}：{exc}") from exc
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in KNOWN_ENV_KEYS and value and key not in environ:
            environ[key] = value
            loaded += 1
    return loaded


def _env(environ: Mapping[str, str], key: str) -> Optional[str]:
    value = environ.get(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _optional_path(value: Optional[str]) -> Optional[Path]:
    return Path(value).expanduser() if value else None


def _check_required(environ: Mapping[str, str], dry_run: bool, need_kindle: bool) -> None:
    keys = list(REQUIRED_ALWAYS) if need_kindle else []
    if not dry_run:
        keys.extend(REQUIRED_UNLESS_DRY)
    missing = [k for k in keys if _env(environ, k) is None]
    if missing:
        raise ConfigError(f"缺少必要環境變數：{', '.join(missing)}（請參考 .env.example）")


def load_config(
    dry_run: bool = False,
    environ: Optional[Mapping[str, str]] = None,
    detect_tools: bool = True,
    need_kindle: bool = True,
) -> Config:
    """組出 Config；缺必要值或偵測失敗即拋 ConfigError。"""
    env = os.environ if environ is None else environ
    _check_required(env, dry_run, need_kindle)
    kindle = _env(env, "KINDLE_REPO")
    kindle_path = Path(kindle).expanduser() if kindle else None
    if need_kindle and (kindle_path is None or not kindle_path.is_dir()):
        raise ConfigError(f"KINDLE_REPO 不是有效目錄：{kindle}")
    cfg = Config(
        kindle_repo=kindle_path,
        work_dir=Path(_env(env, "WORK_DIR") or Path.home() / "skillvideo-work").expanduser(),
        yt_token_path=_optional_path(_env(env, "YT_TOKEN_PATH")),
        yt_playlist_id=_env(env, "YT_PLAYLIST_ID"),
        telegram_bot_token=_env(env, "TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_env(env, "TELEGRAM_CHAT_ID"),
        gmail_user=_env(env, "GMAIL_USER"),
        gmail_app_password=_env(env, "GMAIL_APP_PASSWORD"),
        notify_email=_env(env, "NOTIFY_EMAIL"),
        tts_voice=_env(env, "TTS_VOICE") or DEFAULT_TTS_VOICE,
        tts_rate=_env(env, "TTS_RATE") or DEFAULT_TTS_RATE,
        dry_run=dry_run,
    )
    if detect_tools:
        _attach_tools(cfg, env)
    return cfg


def _attach_tools(cfg: Config, env: Mapping[str, str]) -> None:
    cfg.claude_bin = resolve_claude_bin(env)
    cfg.chrome_bin = detect_chrome(env)
    cfg.font_family, cfg.font_path, cfg.font_index = detect_font(env)


# ---- 平台偵測 ----
def is_windows(system: Optional[str] = None) -> bool:
    return (system or platform.system()) == "Windows"


def detect_font(
    environ: Optional[Mapping[str, str]] = None,
    system: Optional[str] = None,
    exists: Callable[[Path], bool] = Path.is_file,
) -> tuple[str, Path, int]:
    """回傳（CSS 字型家族、Pillow 用字型檔、ttc 索引）；找不到就報錯。"""
    env = os.environ if environ is None else environ
    if is_windows(system):
        windir = Path(env.get("WINDIR") or env.get("SystemRoot") or "C:\\Windows")
        dirs = [windir / "Fonts"]
        local = env.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
        for folder in dirs:
            for name in WINDOWS_FONT_FILES:
                if exists(folder / name):
                    return WINDOWS_FONT_FAMILY, folder / name, 0
        raise ConfigError("找不到微軟正黑體（msjhbd.ttc / msjh.ttc）")
    for candidate in LINUX_FONT_CANDIDATES:
        if exists(Path(candidate)):
            return LINUX_FONT_FAMILY, Path(candidate), LINUX_FONT_INDEX
    raise ConfigError("找不到 Noto Sans CJK 字型，請安裝 fonts-noto-cjk")


def detect_chrome(
    environ: Optional[Mapping[str, str]] = None,
    system: Optional[str] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
    exists: Callable[[Path], bool] = Path.is_file,
) -> str:
    """回傳 Chrome / Chromium 執行檔路徑；CHROME_BIN 優先。"""
    env = os.environ if environ is None else environ
    override = _env(env, "CHROME_BIN")
    if override:
        if not exists(Path(override)):
            raise ConfigError(f"CHROME_BIN 指定的檔案不存在：{override}")
        return override
    if is_windows(system):
        for base_key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = env.get(base_key)
            if base:
                candidate = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
                if exists(candidate):
                    return str(candidate)
        found = which("chrome")
    else:
        found = next((p for p in (which(n) for n in LINUX_CHROME_NAMES) if p), None)
    if not found:
        raise ConfigError("找不到 Chrome / Chromium，請安裝或設定 CHROME_BIN")
    return found


def claude_cmd_to_exe(cmd_path: str) -> Path:
    """npm 版 claude.cmd 只是批次檔包裝，shell=False 無法執行，改指向真正的 claude.exe。"""
    return Path(cmd_path).parent / CLAUDE_EXE_RELATIVE


def _resolve_cmd(path: str, exists: Callable[[Path], bool]) -> str:
    if not path.lower().endswith(".cmd"):
        return path
    exe = claude_cmd_to_exe(path)
    if not exists(exe):
        raise ConfigError(f"由 {path} 推導的 claude.exe 不存在：{exe}")
    return str(exe)


def resolve_claude_bin(
    environ: Optional[Mapping[str, str]] = None,
    system: Optional[str] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
    exists: Callable[[Path], bool] = Path.is_file,
) -> str:
    """找 claude CLI；Windows 優先 claude.exe，若只有 claude.cmd 就解析到 node_modules 內的 exe。"""
    env = os.environ if environ is None else environ
    override = _env(env, "CLAUDE_BIN")
    if override:
        return _resolve_cmd(override, exists)
    if not is_windows(system):
        found = which("claude")
        if not found:
            raise ConfigError("找不到 claude CLI，請安裝或設定 CLAUDE_BIN")
        return found
    exe = which("claude.exe")
    if exe:
        return exe
    cmd = which("claude.cmd") or which("claude")
    if not cmd:
        raise ConfigError("找不到 claude CLI（claude.exe / claude.cmd），請設定 CLAUDE_BIN")
    return _resolve_cmd(cmd, exists)


# ---- 外部指令 ----
def tail_text(text: Optional[str], lines: int = 20) -> str:
    """取文字最後 N 行（錯誤摘要用）。"""
    if not text:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])


# 傳給 claude 子程序前要剝掉的機密：通知 / YouTube 憑證，以及任何 Anthropic 金鑰類變數（避免改走 API 計費）
SECRET_ENV_PREFIXES = ("GMAIL_", "TELEGRAM_", "YT_")
ANTHROPIC_PREFIX = "ANTHROPIC_"
ANTHROPIC_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET")


def is_secret_env_key(key: str) -> bool:
    """判斷環境變數名稱是否屬於不可外洩給子程序的機密（只看名稱，不讀值）。"""
    upper = key.upper()
    if upper.startswith(SECRET_ENV_PREFIXES):
        return True
    return upper.startswith(ANTHROPIC_PREFIX) and any(m in upper for m in ANTHROPIC_SECRET_MARKERS)


def filtered_child_env(environ: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """複製環境變數並移除機密鍵，給 claude 等外部 CLI 使用。"""
    env = os.environ if environ is None else environ
    return {k: v for k, v in env.items() if not is_secret_env_key(k)}


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """逾時時連同子孫程序一起砍掉（Chrome 會 fork 多個 renderer，只砍父程序會殘留）。"""
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                           encoding="utf-8", errors="replace", timeout=30, check=False)
    except (ProcessLookupError, PermissionError, OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("砍除程序樹失敗（pid %s）：%s", proc.pid, exc)
    if proc.poll() is None:
        proc.kill()


def _communicate(proc: subprocess.Popen, input_text: Optional[str], timeout: float, name: str) -> tuple[str, str]:
    try:
        return proc.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_process_tree(proc)
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            logger.warning("%s 被砍後仍未結束", name)
        raise ExternalCommandError(f"{name} 執行逾時（{timeout} 秒）") from exc


def run_external(
    cmd: Sequence[str],
    timeout: float,
    input_text: Optional[str] = None,
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> subprocess.CompletedProcess:
    """執行外部指令（不經 shell、UTF-8、必有 timeout、逾時砍整個程序樹），失敗拋 ExternalCommandError。"""
    args = [str(c) for c in cmd]
    name = Path(args[0]).name
    try:
        proc = subprocess.Popen(
            args, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
            cwd=str(cwd) if cwd else None, env=dict(env) if env is not None else None,
            shell=False, start_new_session=(os.name == "posix"),
        )
    except OSError as exc:
        raise ExternalCommandError(f"{name} 無法執行：{exc}") from exc
    stdout, stderr = _communicate(proc, input_text, timeout, name)
    if proc.returncode != 0:
        detail = tail_text(stderr) or tail_text(stdout)
        raise ExternalCommandError(f"{name} 結束碼 {proc.returncode}：{detail}")
    return subprocess.CompletedProcess(args, proc.returncode, stdout=stdout, stderr=stderr)
