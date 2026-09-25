"""渲染：投影片 PNG（Chrome headless）、TTS（edge-tts）、字幕畫面（Pillow）、ffmpeg 合成、縮圖、SRT。

做法沿用 p169 Demo 已驗證版本。
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import shutil
import tempfile
import textwrap
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from PIL import Image, ImageDraw, ImageFont

from .config import Config, ExternalCommandError, SERIES_NAME, is_windows, run_external

logger = logging.getLogger(__name__)

# ---- 版面常數（對應 templates/slide.css）----
SLIDE_W, SLIDE_H = 1920, 1080
PRE_INNER_W = 1920 - 100 * 2 - 54 * 2 - 4   # 左右 100px 邊距、54px padding、2px 邊框
PRE_INNER_H = 560 - 40 * 2 - 4              # 高 560、上下 40px padding、2px 邊框
MIN_CODE_FONT = 24
ASCII_EM = 0.55                              # Consolas 半形字寬約 0.55em

# ---- 語音 / 長度 ----
SENTENCE_PAUSE = 0.4
TTS_RETRIES = 3
TTS_RETRY_INTERVAL = 2.0
TTS_SENTENCE_GAP = 0.5                       # 每句間隔，避免 edge-tts 限流
TARGET_MIN, TARGET_MAX, TARGET_MID = 285.0, 315.0, 300.0
RATE_MIN, RATE_MAX = -10, 20

# ---- 字幕 ----
SUB_FONT_SIZE = 50
SUB_WRAP = 30
SUB_LINE_H = 66
SUB_BOTTOM = 60
SUB_BG = (8, 12, 22, 215)

FFMPEG_SEG_TIMEOUT = 180
FFMPEG_CONCAT_TIMEOUT = 600
CHROME_TIMEOUT = 90

KEYWORDS = frozenset(
    "for in while if else elif def return import from class function const let var async await "
    "with as try except finally raise True False None null true false new export default then fi do done "
    "echo cd npm npx pip python node".split()
)
_TOKEN_RE = re.compile(
    r"(?P<c>#[^\n]*|//[^\n]*)"
    r"|(?P<s>\"[^\"\n]*\"|'[^'\n]*')"
    r"|(?P<w>[A-Za-z_][A-Za-z0-9_]*)(?P<call>(?=\())?"
)


class RenderError(Exception):
    """渲染流程失敗。"""


@dataclass
class RenderResult:
    """渲染產物。"""

    video_path: Path
    srt_path: Path
    thumb_path: Path
    duration: float
    rate: str
    slides: list[Path] = field(default_factory=list)
    sentence_count: int = 0


# ---- 程式碼框字級與溢出 ----
def line_em_width(line: str) -> float:
    """估算一行在等寬字型下的寬度（em）：全形 1em、半形 0.55em。"""
    width = 0.0
    for ch in line.replace("\t", "    "):
        width += 1.0 if unicodedata.east_asian_width(ch) in ("W", "F") else ASCII_EM
    return width


def code_font_size(line_count: int) -> int:
    """SPEC §8：≤8 行 40px、≤10 行 34px、其餘 30px。"""
    if line_count <= 8:
        return 40
    if line_count <= 10:
        return 34
    return 30


def code_line_height(line_count: int) -> float:
    # 10 行 × 34px × 1.45 = 493px 會超出框內 476px，9 行以上改用較緊的行高
    return 1.45 if line_count <= 8 else 1.35


def fit_code_font(code: str) -> tuple[int, float]:
    """依行數取字級，若最寬一行仍超出框寬就再縮（最小 24px）。"""
    lines = code.splitlines() or [""]
    size = code_font_size(len(lines))
    widest = max(line_em_width(ln) for ln in lines)
    while size > MIN_CODE_FONT and widest * size > PRE_INNER_W:
        size -= 2
    return size, code_line_height(len(lines))


def code_overflows(code: Optional[str]) -> bool:
    """以行數 / 字寬規則近似判斷程式碼框是否溢出（SPEC §9-2）。"""
    if not code:
        return False
    lines = code.splitlines() or [""]
    size, line_h = fit_code_font(code)
    widest = max(line_em_width(ln) for ln in lines)
    return len(lines) * size * line_h > PRE_INNER_H or widest * size > PRE_INNER_W


# ---- 投影片 HTML ----
def _span(cls: str, text: str) -> str:
    return f"<span class={cls}>{html.escape(text)}</span>"


def highlight_code(code: str) -> str:
    """簡易上色：註解 / 字串 / 關鍵字 / 函式呼叫，其餘文字 HTML 跳脫。"""
    out, pos = [], 0
    for match in _TOKEN_RE.finditer(code):
        out.append(html.escape(code[pos:match.start()]))
        pos = match.end()
        if match.group("c"):
            out.append(_span("c", match.group("c")))
        elif match.group("s"):
            out.append(_span("s", match.group("s")))
        elif match.group("call") is not None:
            out.append(_span("f", match.group("w")))
        elif match.group("w") in KEYWORDS:
            out.append(_span("k", match.group("w")))
        else:
            out.append(html.escape(match.group("w")))
    out.append(html.escape(code[pos:]))
    return "".join(out)


def fit_heading_px(text: str, base: int, avail_px: int) -> int:
    """標題太長時縮字級，避免超出版面。"""
    width = line_em_width(text) or 1.0
    return max(40, min(base, int(avail_px / width)))


def _font_override(font_family: str) -> str:
    fam = html.escape(font_family, quote=True)
    return (f"body{{font-family:'{fam}',sans-serif}}"
            f"pre{{font-family:Consolas,'DejaVu Sans Mono','{fam}',monospace}}")


def _cover_body(scene: dict[str, Any], ep: int) -> str:
    h1_px = fit_heading_px(scene["t"], 128, 1700)
    return (f"<div class=t><div class=py>{html.escape(SERIES_NAME.upper())} · 第 {ep} 集</div>"
            f"<h1 style='font-size:{h1_px}px'>{html.escape(scene['t'])}</h1>"
            f"<p>{html.escape(scene.get('sub') or '')}</p></div>")


def _content_body(scene: dict[str, Any]) -> str:
    h1_px = fit_heading_px(scene["t"], 76, 1720)
    body = (f"<div class=h><h1 style='font-size:{h1_px}px'>{html.escape(scene['t'])}</h1>"
            f"<p>{html.escape(scene.get('sub') or '')}</p></div>")
    code = scene.get("code")
    if code:
        size, line_h = fit_code_font(code)
        body += f"<pre style='font-size:{size}px;line-height:{line_h}'>{highlight_code(code)}</pre>"
    return body


def build_slide_html(scene: dict[str, Any], ep: int, index: int, css: str, font_family: str) -> str:
    """第 0 段用封面版型（.t），其餘用 .h 標題 + <pre> 程式碼框；右上角顯示 #001。"""
    body = _cover_body(scene, ep) if index == 0 else _content_body(scene)
    return ("<!DOCTYPE html><html lang=zh-TW><head><meta charset=UTF-8>"
            f"<style>{css}\n{_font_override(font_family)}</style></head><body>"
            f"<div class=bar></div><div class=ep>#{ep:03d}</div>{body}</body></html>")


def chrome_command(chrome_bin: str, html_path: Path, png_path: Path, profile_dir: Path) -> list[str]:
    """SPEC §8 的 Chrome 參數；另加獨立 profile，避免與使用者開著的 Chrome 搶同一個設定檔。"""
    cmd = [chrome_bin, "--headless=new", "--disable-gpu", "--hide-scrollbars",
           "--force-device-scale-factor=1", f"--window-size={SLIDE_W},{SLIDE_H}",
           "--no-first-run", f"--user-data-dir={profile_dir}",
           f"--screenshot={png_path}", html_path.resolve().as_uri()]
    # root 身分執行 Chromium 必須關 sandbox（VPS 若以 root 跑才會用到）
    if not is_windows() and hasattr(os, "geteuid") and os.geteuid() == 0:
        cmd.insert(1, "--no-sandbox")
    return cmd


def render_slides(cfg: Config, scenes: Sequence[dict[str, Any]], ep: int, slides_dir: Path) -> list[Path]:
    """每段輸出一張 1920x1080 PNG。"""
    if not cfg.chrome_bin or not cfg.font_family:
        raise RenderError("缺少 Chrome 或字型設定")
    css = cfg.css_path.read_text(encoding="utf-8")
    slides_dir.mkdir(parents=True, exist_ok=True)
    pngs = []
    # Windows 上 Chrome 剛結束時 profile 檔可能仍被鎖，清不掉不該讓整集失敗
    with tempfile.TemporaryDirectory(prefix="skillvideo-chrome-", ignore_cleanup_errors=True) as profile:
        for idx, scene in enumerate(scenes):
            html_path = slides_dir / f"s{idx:02d}.html"
            png_path = slides_dir / f"s{idx:02d}.png"
            html_path.write_text(build_slide_html(scene, ep, idx, css, cfg.font_family), encoding="utf-8")
            run_external(chrome_command(cfg.chrome_bin, html_path, png_path, Path(profile)),
                         timeout=CHROME_TIMEOUT)
            if not png_path.is_file() or png_path.stat().st_size == 0:
                raise RenderError(f"Chrome 未產生投影片：{png_path.name}")
            pngs.append(png_path)
    return pngs


# ---- 語音 ----
def parse_rate(rate: str) -> int:
    match = re.fullmatch(r"\s*([+-]?\d+)%\s*", rate or "")
    if not match:
        raise RenderError(f"TTS_RATE 格式錯誤（應為 +8% 之類）：{rate}")
    return int(match.group(1))


def format_rate(pct: int) -> str:
    return f"{pct:+d}%"


def total_duration(durations: Sequence[float]) -> float:
    """總長 = 每句長度 + 0.4 秒停頓。"""
    return sum(d + SENTENCE_PAUSE for d in durations)


def compute_adjusted_rate(current_pct: int, total: float, sentence_count: int) -> Optional[int]:
    """總長不在 285～315 秒時，推算讓總長接近 300 秒的語速（限制在 -10%～+20%）；不需調整回傳 None。"""
    if TARGET_MIN <= total <= TARGET_MAX:
        return None
    pauses = SENTENCE_PAUSE * sentence_count
    speech, target_speech = total - pauses, TARGET_MID - pauses
    if speech <= 0 or target_speech <= 0:
        return None
    # 語音長度與 (1 + rate) 成反比
    new_factor = (1 + current_pct / 100) * speech / target_speech
    new_pct = max(RATE_MIN, min(RATE_MAX, round((new_factor - 1) * 100)))
    return None if new_pct == current_pct else new_pct


def _default_communicate(text: str, voice: str, rate: str) -> Any:
    import edge_tts  # 延遲載入：測試與 verify 子命令不需要

    return edge_tts.Communicate(text, voice, rate=rate)


def _tts_errors() -> tuple[type[BaseException], ...]:
    errors: list[type[BaseException]] = [OSError, asyncio.TimeoutError, ValueError]
    try:
        import aiohttp
        from edge_tts.exceptions import EdgeTTSException

        errors += [aiohttp.ClientError, EdgeTTSException]
    except ImportError:
        pass
    return tuple(errors)


async def _synth_one(text: str, path: Path, voice: str, rate: str, factory: Callable[..., Any]) -> None:
    errors = _tts_errors()
    for attempt in range(1, TTS_RETRIES + 1):
        try:
            await factory(text, voice, rate=rate).save(str(path))
            if path.is_file() and path.stat().st_size > 0:
                return
            raise OSError("edge-tts 產出空檔")
        except errors as exc:
            logger.warning("TTS 失敗（第 %d/%d 次）%s：%s", attempt, TTS_RETRIES, path.name, exc)
            if attempt < TTS_RETRIES:
                await asyncio.sleep(TTS_RETRY_INTERVAL)
    raise RenderError(f"TTS 連續 {TTS_RETRIES} 次失敗：{text[:20]}")


async def _synth_all(items: Sequence[tuple[str, Path]], voice: str, rate: str, factory: Callable[..., Any]) -> None:
    for text, path in items:
        await _synth_one(text, path, voice, rate, factory)
        await asyncio.sleep(TTS_SENTENCE_GAP)


def synthesize(items: Sequence[tuple[str, Path]], voice: str, rate: str,
               factory: Optional[Callable[..., Any]] = None) -> None:
    """逐句產生 mp3。"""
    asyncio.run(_synth_all(items, voice, rate, factory or _default_communicate))


def probe_duration(path: Path) -> float:
    """用 ffprobe 量媒體長度（秒）。"""
    proc = run_external(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                         "-of", "default=noprint_wrappers=1:nokey=1", str(path)], timeout=60)
    try:
        return float(proc.stdout.strip())
    except ValueError as exc:
        raise RenderError(f"ffprobe 無法取得長度：{path.name}") from exc


def synthesize_calibrated(cfg: Config, sentences: Sequence[str], audio_dir: Path) -> tuple[list[float], str]:
    """產生語音並量長度；總長超出 285～315 秒時調語速重產一次（仍超出交給 QA）。"""
    audio_dir.mkdir(parents=True, exist_ok=True)
    items = [(text, audio_dir / f"n{i:03d}.mp3") for i, text in enumerate(sentences)]
    rate = cfg.tts_rate
    synthesize(items, cfg.tts_voice, rate)
    durations = [probe_duration(p) for _, p in items]
    new_pct = compute_adjusted_rate(parse_rate(rate), total_duration(durations), len(items))
    if new_pct is not None:
        rate = format_rate(new_pct)
        logger.info("總長 %.1f 秒超出目標，改用語速 %s 重產", total_duration(durations), rate)
        synthesize(items, cfg.tts_voice, rate)
        durations = [probe_duration(p) for _, p in items]
    return durations, rate


# ---- 字幕 ----
def wrap_subtitle(text: str, width: int = SUB_WRAP) -> list[str]:
    """每 30 字換行。"""
    return textwrap.wrap(text.strip(), width) or [""]


def draw_subtitle(slide_png: Path, text: str, out_path: Path, font_path: Path, font_index: int = 0) -> None:
    """在投影片底部畫半透明黑底白字字幕（50px，行高 66px，最後一行底距 60px）。"""
    lines = wrap_subtitle(text)
    try:
        font = ImageFont.truetype(str(font_path), SUB_FONT_SIZE, index=font_index)
        base = Image.open(slide_png).convert("RGBA")
    except OSError as exc:
        raise RenderError(f"字幕畫面失敗：{exc}") from exc
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    first_top = base.height - SUB_BOTTOM - SUB_LINE_H * len(lines)
    draw.rectangle([0, first_top - 24, base.width, base.height], fill=SUB_BG)
    for i, line in enumerate(lines):
        width = draw.textlength(line, font=font)
        y = first_top + i * SUB_LINE_H + (SUB_LINE_H - SUB_FONT_SIZE) // 2
        draw.text(((base.width - width) / 2, y), line, font=font, fill=(255, 255, 255, 255))
    Image.alpha_composite(base, overlay).convert("RGB").save(out_path)


def format_srt_time(seconds: float) -> str:
    """秒數 → SRT 時間格式 HH:MM:SS,mmm。"""
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def build_srt(sentences: Sequence[str], durations: Sequence[float]) -> str:
    """每句一條字幕，句與句之間的 0.4 秒停頓不顯示字幕。"""
    blocks, cursor = [], 0.0
    for idx, (text, dur) in enumerate(zip(sentences, durations), start=1):
        start, end = cursor, cursor + dur
        blocks.append(f"{idx}\n{format_srt_time(start)} --> {format_srt_time(end)}\n{text}\n")
        cursor = end + SENTENCE_PAUSE
    return "\n".join(blocks)


# ---- ffmpeg ----
def segment_command(frame: Path, audio: Path, duration: float, out: Path) -> list[str]:
    padded = f"{duration + SENTENCE_PAUSE:.3f}"
    return ["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-framerate", "30",
            "-i", str(frame), "-i", str(audio), "-af", f"apad=pad_dur={SENTENCE_PAUSE}",
            "-t", padded, "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", str(out)]


def concat_segments(segments: Sequence[Path], out: Path) -> None:
    """串接片段；concat 清單用相對檔名並在片段目錄執行，避開路徑跳脫問題。"""
    seg_dir = segments[0].parent
    listing = "".join(f"file '{p.name}'\n" for p in segments)
    (seg_dir / "concat.txt").write_text(listing, encoding="utf-8")
    run_external(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                  "-i", "concat.txt", "-c", "copy", "-movflags", "+faststart", str(out.resolve())],
                 timeout=FFMPEG_CONCAT_TIMEOUT, cwd=seg_dir)


def make_thumbnail(cover_png: Path, out: Path) -> None:
    """封面投影片縮成 1280x720 JPG。"""
    try:
        with Image.open(cover_png) as img:
            img.convert("RGB").resize((1280, 720), Image.LANCZOS).save(out, "JPEG", quality=85)
    except OSError as exc:
        raise RenderError(f"縮圖產生失敗：{exc}") from exc


INTERMEDIATE_DIRS = ("frames", "segs", "audio", "slides")


def cleanup_intermediates(ep_dir: Path) -> list[str]:
    """上傳成功後刪中間產物，只留 mp4、srt、縮圖、QA 截圖（與 scenes.json）；回傳已刪目錄名。"""
    removed = []
    for name in INTERMEDIATE_DIRS:
        target = ep_dir / name
        if not target.is_dir():
            continue
        try:
            shutil.rmtree(target)
            removed.append(name)
        except OSError as exc:
            logger.warning("刪除中間產物 %s 失敗：%s", target, exc)
    return removed


# ---- 主入口 ----
def flatten_sentences(scenes: Sequence[dict[str, Any]]) -> list[tuple[int, str]]:
    """展開成（段落索引, 旁白句）清單。"""
    return [(idx, text) for idx, scene in enumerate(scenes) for text in scene["n"]]


def _build_segments(cfg: Config, flat: Sequence[tuple[int, str]], slides: Sequence[Path],
                    durations: Sequence[float], ep_dir: Path) -> list[Path]:
    frames_dir, segs_dir = ep_dir / "frames", ep_dir / "segs"
    frames_dir.mkdir(exist_ok=True)
    segs_dir.mkdir(exist_ok=True)
    segments = []
    for i, ((scene_idx, text), dur) in enumerate(zip(flat, durations)):
        frame, seg = frames_dir / f"f{i:03d}.png", segs_dir / f"seg{i:03d}.mp4"
        draw_subtitle(slides[scene_idx], text, frame, cfg.font_path, cfg.font_index)
        run_external(segment_command(frame, ep_dir / "audio" / f"n{i:03d}.mp3", dur, seg),
                     timeout=FFMPEG_SEG_TIMEOUT)
        segments.append(seg)
    return segments


def render_episode(cfg: Config, script: dict[str, Any], ep: int, ep_dir: Path) -> RenderResult:
    """完整渲染一集：投影片 → 語音 → 字幕畫面 → 片段 → 串接 → SRT → 縮圖。"""
    if cfg.font_path is None:
        raise RenderError("缺少字型設定")
    scenes = script["scenes"]
    flat = flatten_sentences(scenes)
    try:
        slides = render_slides(cfg, scenes, ep, ep_dir / "slides")
        durations, rate = synthesize_calibrated(cfg, [t for _, t in flat], ep_dir / "audio")
        segments = _build_segments(cfg, flat, slides, durations, ep_dir)
        video = ep_dir / f"ep{ep:03d}.mp4"
        concat_segments(segments, video)
    except ExternalCommandError as exc:
        raise RenderError(str(exc)) from exc
    srt = ep_dir / f"ep{ep:03d}.srt"
    srt.write_text(build_srt([t for _, t in flat], durations), encoding="utf-8")
    thumb = ep_dir / "thumb.jpg"
    make_thumbnail(slides[0], thumb)
    (ep_dir / "durations.json").write_text(json.dumps(durations), encoding="utf-8")
    return RenderResult(video_path=video, srt_path=srt, thumb_path=thumb,
                        duration=total_duration(durations), rate=rate, slides=slides,
                        sentence_count=len(flat))
