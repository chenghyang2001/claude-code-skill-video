"""render：程式碼字級規則、溢出判斷、上色、投影片 HTML、字幕換行、SRT、語速校正、TTS 重試、ffmpeg 參數。"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from skillvideo import render
from skillvideo.config import ConfigError, detect_font


def test_code_font_size_rule():
    assert render.code_font_size(1) == 40
    assert render.code_font_size(8) == 40
    assert render.code_font_size(9) == 34
    assert render.code_font_size(10) == 34
    assert render.code_font_size(11) == 30


def test_fit_code_font_shrinks_wide_cjk_lines():
    size, _ = render.fit_code_font("短")
    assert size == 40
    wide_size, _ = render.fit_code_font("中" * 48)
    assert wide_size < 40
    assert 48 * wide_size <= render.PRE_INNER_W or wide_size == render.MIN_CODE_FONT


def test_code_overflow_rules():
    assert not render.code_overflows(None)
    assert not render.code_overflows("\n".join(["x = 1"] * 10))
    assert render.code_overflows("\n".join(["x = 1"] * 12))
    assert render.code_overflows("中" * 80)


def test_highlight_code_colors_and_escapes():
    out = render.highlight_code('# 註解\nfor i in range(3):\n    print("<b>")')
    assert "<span class=c># 註解</span>" in out
    assert "<span class=k>for</span>" in out
    assert "<span class=f>range</span>" in out
    assert "<span class=s>&quot;&lt;b&gt;&quot;</span>" in out
    assert "<b>" not in out


def test_build_slide_html_cover_and_content(cfg):
    css = cfg.css_path.read_text(encoding="utf-8")
    cover = render.build_slide_html({"t": "paperjsx", "sub": "一句話", "code": None, "n": []}, 1, 0, css, "Noto Sans CJK TC")
    content = render.build_slide_html({"t": "怎麼觸發", "sub": "指令", "code": "npm i x", "n": []}, 1, 1, css, "Noto Sans CJK TC")
    assert "class=t" in cover and "<pre" not in cover
    assert "class=h" in content and "<pre style='font-size:40px" in content
    assert "#001" in cover and "#001" in content
    assert "Noto Sans CJK TC" in content


def test_wrap_subtitle_every_30_chars():
    lines = render.wrap_subtitle("一" * 40)
    assert [len(x) for x in lines] == [30, 10]
    assert render.wrap_subtitle("") == [""]


def test_srt_time_format():
    assert render.format_srt_time(0) == "00:00:00,000"
    assert render.format_srt_time(3661.2345) == "01:01:01,234"
    assert render.format_srt_time(59.9996) == "00:01:00,000"


def test_build_srt_uses_pause_between_sentences():
    srt = render.build_srt(["第一句", "第二句"], [1.5, 2.0])
    blocks = srt.strip().split("\n\n")
    assert blocks[0].splitlines() == ["1", "00:00:00,000 --> 00:00:01,500", "第一句"]
    assert blocks[1].splitlines()[1] == "00:00:01,900 --> 00:00:03,900"


def test_rate_parse_and_format():
    assert render.parse_rate("+8%") == 8
    assert render.parse_rate("-5%") == -5
    assert render.format_rate(0) == "+0%"
    assert render.format_rate(-10) == "-10%"
    with pytest.raises(render.RenderError):
        render.parse_rate("fast")


def test_compute_adjusted_rate():
    assert render.compute_adjusted_rate(8, 300.0, 60) is None
    assert render.compute_adjusted_rate(8, 400.0, 60) == 20       # 太長 → 上限 +20%
    assert render.compute_adjusted_rate(8, 200.0, 60) == -10      # 太短 → 下限 -10%
    moderate = render.compute_adjusted_rate(8, 320.0, 60)
    assert moderate is not None and 8 < moderate <= 20
    assert render.total_duration([1.0, 2.0]) == pytest.approx(3.8)


def test_synthesize_retries_then_succeeds(tmp_path, monkeypatch):
    attempts = {"n": 0}

    class FakeCommunicate:
        def __init__(self, text, voice, rate):
            self.rate = rate

        async def save(self, path):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise OSError("network")
            Path(path).write_bytes(b"mp3")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(render.asyncio, "sleep", no_sleep)
    out = tmp_path / "a.mp3"
    render.synthesize([("你好", out)], "voice", "+8%", factory=FakeCommunicate)
    assert out.read_bytes() == b"mp3"
    assert attempts["n"] == 3


def test_synthesize_gives_up_after_three(tmp_path, monkeypatch):
    class Broken:
        def __init__(self, *a, **k):
            pass

        async def save(self, path):
            raise OSError("down")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(render.asyncio, "sleep", no_sleep)
    with pytest.raises(render.RenderError, match="3 次"):
        render.synthesize([("你好", tmp_path / "a.mp3")], "v", "+0%", factory=Broken)


def test_segment_and_chrome_commands(tmp_path):
    seg = render.segment_command(Path("f.png"), Path("a.mp3"), 2.5, Path("s.mp4"))
    assert seg[seg.index("-t") + 1] == "2.900"
    assert "apad=pad_dur=0.4" in seg and "stillimage" in seg and "yuv420p" in seg
    chrome = render.chrome_command("chrome", tmp_path / "s.html", tmp_path / "s.png", tmp_path / "prof")
    assert "--headless=new" in chrome and "--window-size=1920,1080" in chrome
    assert chrome[-1].startswith("file:")


def test_draw_subtitle_outputs_same_size_image(tmp_path):
    try:
        _, font_path, index = detect_font()
    except ConfigError:
        pytest.skip("本機沒有 CJK 字型")
    slide = tmp_path / "s.png"
    Image.new("RGB", (1920, 1080), (30, 60, 90)).save(slide)
    out = tmp_path / "f.png"
    render.draw_subtitle(slide, "這是一句很長的字幕" * 5, out, font_path, index)
    with Image.open(out) as img:
        assert img.size == (1920, 1080)
        assert img.getpixel((960, 1075)) != (30, 60, 90)   # 底部被字幕底色覆蓋
        assert img.getpixel((960, 100)) == (30, 60, 90)    # 上方維持原樣


def test_cleanup_intermediates_keeps_deliverables(tmp_path):
    for name in ("frames", "segs", "audio", "slides", "qa"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "x.bin").write_bytes(b"x")
    for name in ("ep001.mp4", "ep001.srt", "thumb.jpg", "scenes.json"):
        (tmp_path / name).write_bytes(b"x")
    removed = render.cleanup_intermediates(tmp_path)
    assert sorted(removed) == ["audio", "frames", "segs", "slides"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ep001.mp4", "ep001.srt", "qa", "scenes.json", "thumb.jpg"]
