"""品質閘門（SPEC §9）：任一項不過 → 該集 failed，不上傳。"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import ExternalCommandError, run_external
from .render import RenderResult, code_overflows

logger = logging.getLogger(__name__)

VIDEO_MIN, VIDEO_MAX = 270.0, 330.0
VIDEO_W, VIDEO_H = 1920, 1080
SCREENSHOT_POINTS = (0.10, 0.50, 0.90)

SENSITIVE_PATTERNS = (
    ("Windows 使用者路徑", re.compile(r"C:\\+Users", re.I)),
    ("Windows 使用者路徑", re.compile(r"C:/Users", re.I)),
    ("Linux 家目錄路徑", re.compile(r"/home/")),
    ("root 家目錄路徑", re.compile(r"/root/")),
    ("Git Bash 使用者路徑", re.compile(r"/c/Users", re.I)),
    ("macOS 使用者路徑", re.compile(r"(?<![A-Za-z0-9_])/Users/")),
    ("Claude 設定目錄", re.compile(r"~/\.claude")),
    ("Telegram bot token", re.compile(r"\d{8,}:AA[\w-]{30,}")),
    ("Slack token", re.compile(r"\bxox[bp]-[A-Za-z0-9-]{6,}")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("GitHub token", re.compile(r"\bgh[opsur]_[A-Za-z0-9]{6,}")),
    ("API key（sk-）", re.compile(r"\bsk-[A-Za-z0-9_-]{6,}")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_-]{10,}")),
)
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
# 單一位數字太常見（「第 1 步」），只有後面緊接這些單位時才視為 Demo 的數據
_UNIT_RE = re.compile(r"^\s?(天|個|次|秒|分|小時|KB|MB|GB|%|萬|千|session|token|turn|筆|則|頁)", re.I)
_PROJECT_RE = re.compile(r"專案(?:名稱)?[：:]\s*`?([^\s`，。、]+)")


class QAError(Exception):
    """QA 執行本身失敗（不是檢查不過）。"""


@dataclass
class QAResult:
    """QA 結果。"""

    passed: bool
    errors: list[str] = field(default_factory=list)
    screenshots: list[Path] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        if self.passed:
            return (f"QA 通過：{self.meta.get('duration', 0):.1f} 秒、"
                    f"{self.meta.get('width')}x{self.meta.get('height')}、音軌 {'有' if self.meta.get('has_audio') else '無'}")
        return "QA 未通過：\n- " + "\n- ".join(self.errors)


# ---- 影片規格 ----
def probe_video(path: Path) -> dict[str, Any]:
    """ffprobe 取長度、解析度、是否有音軌。"""
    proc = run_external(["ffprobe", "-v", "error", "-show_entries",
                         "stream=codec_type,width,height:format=duration", "-of", "json", str(path)],
                        timeout=60)
    try:
        info = json.loads(proc.stdout)
        streams = info.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), {})
        return {"duration": float(info["format"]["duration"]),
                "width": video.get("width"), "height": video.get("height"),
                "has_audio": any(s.get("codec_type") == "audio" for s in streams)}
    except (ValueError, KeyError, TypeError) as exc:
        raise QAError(f"ffprobe 輸出無法解析：{exc}") from exc


def check_video_meta(meta: dict[str, Any]) -> list[str]:
    """長度 270～330 秒、1920x1080、有音軌。"""
    errors = []
    duration = float(meta.get("duration") or 0)
    if not VIDEO_MIN <= duration <= VIDEO_MAX:
        errors.append(f"影片長度 {duration:.1f} 秒不在 {VIDEO_MIN:.0f}～{VIDEO_MAX:.0f} 秒")
    if (meta.get("width"), meta.get("height")) != (VIDEO_W, VIDEO_H):
        errors.append(f"解析度 {meta.get('width')}x{meta.get('height')} 不是 {VIDEO_W}x{VIDEO_H}")
    if not meta.get("has_audio"):
        errors.append("影片沒有音軌")
    return errors


# ---- 投影片 ----
def check_code_overflow(scenes: Sequence[dict[str, Any]]) -> list[str]:
    return [f"第 {i} 段投影片程式碼框溢出" for i, s in enumerate(scenes) if code_overflows(s.get("code"))]


# ---- 文字檢查 ----
def script_text(script: dict[str, Any], full_title: str = "") -> str:
    """把標題、描述、標籤、投影片與旁白全部串成一段文字。"""
    parts = [full_title, str(script.get("title", "")), str(script.get("description", ""))]
    parts += [str(t) for t in script.get("tags") or []]
    for scene in script.get("scenes") or []:
        parts += [str(scene.get("t", "")), str(scene.get("sub", "")), str(scene.get("code") or "")]
        parts += [str(n) for n in scene.get("n") or []]
    return "\n".join(parts)


def find_sensitive_strings(text: str) -> list[str]:
    """回傳命中的敏感樣式（路徑、email、token）。"""
    hits = []
    for label, pattern in SENSITIVE_PATTERNS:
        match = pattern.search(text)
        if match:
            hits.append(f"{label}：{match.group(0)[:24]}")
    return hits


def _normalize_number(raw: str) -> str:
    return raw.replace(",", "")


def extract_demo_facts(demo_text: str) -> set[str]:
    """抽出 Demo 紀錄中的數據（≥2 位數字、帶單位的數字）與專案名稱。"""
    facts = set()
    for match in _NUMBER_RE.finditer(demo_text):
        number = _normalize_number(match.group(0))
        digits = number.replace(".", "")
        if len(digits) >= 2 or _UNIT_RE.match(demo_text[match.end():match.end() + 8]):
            facts.add(number)
    facts.update(m.group(1) for m in _PROJECT_RE.finditer(demo_text))
    return facts


def find_leaked_facts(text: str, demo_texts: Iterable[str]) -> list[str]:
    """敏感集數：腳本中出現 Demo 紀錄裡的數字 / 專案名 → 回傳命中清單。"""
    facts = set()
    for demo in demo_texts:
        facts |= extract_demo_facts(demo)
    script_numbers = {_normalize_number(m.group(0)) for m in _NUMBER_RE.finditer(text)}
    leaked = sorted(f for f in facts if f in script_numbers or (not f[0].isdigit() and f in text))
    return leaked


def _normalize_title(title: str) -> str:
    return re.sub(r"[\s｜|:：，,。!！?？]+", "", title).lower()


def is_duplicate_title(title: str, previous: Iterable[str]) -> bool:
    """忽略空白與標點後比對，與既往集數標題相同即視為重複。"""
    key = _normalize_title(title)
    return any(_normalize_title(p) == key for p in previous if p)


# ---- 截圖 ----
def extract_screenshots(video: Path, duration: float, out_dir: Path) -> list[Path]:
    """在 10%、50%、90% 截圖（640 寬 JPG，確保 ≤ 300KB 可當信件附件）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    shots = []
    for point in SCREENSHOT_POINTS:
        out = out_dir / f"qa_{int(point * 100):02d}.jpg"
        run_external(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{duration * point:.2f}",
                      "-i", str(video), "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "5", str(out)],
                     timeout=60)
        shots.append(out)
    return shots


# ---- 主入口 ----
def check_texts(script: dict[str, Any], full_title: str, previous: Sequence[str],
                sensitive: bool, demo_texts: Sequence[str]) -> list[str]:
    """文字類檢查：敏感字串、敏感集數數據、標題重複。"""
    text = script_text(script, full_title)
    errors = [f"含敏感字串 {hit}" for hit in find_sensitive_strings(text)]
    if sensitive:
        leaked = find_leaked_facts(text, demo_texts)
        if leaked:
            errors.append(f"敏感集數引用了 Demo 紀錄中的數據：{', '.join(leaked[:10])}")
    if is_duplicate_title(str(script.get("title", "")), previous):
        errors.append(f"標題與既往集數重複：{script.get('title')}")
    return errors


def run_qa(render: RenderResult, script: dict[str, Any], full_title: str, previous_titles: Sequence[str],
           sensitive: bool, demo_texts: Sequence[str], out_dir: Path) -> QAResult:
    """執行全部品質檢查；只有通過時才截圖。"""
    try:
        meta = probe_video(render.video_path)
    except ExternalCommandError as exc:
        raise QAError(f"無法檢查影片：{exc}") from exc
    errors = check_video_meta(meta)
    errors += check_code_overflow(script.get("scenes") or [])
    errors += check_texts(script, full_title, previous_titles, sensitive, demo_texts)
    result = QAResult(passed=not errors, errors=errors, meta=meta)
    if result.passed:
        try:
            result.screenshots = extract_screenshots(render.video_path, meta["duration"], out_dir)
        except ExternalCommandError as exc:
            logger.warning("QA 截圖失敗（不影響判定）：%s", exc)
    return result
