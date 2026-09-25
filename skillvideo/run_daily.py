"""主流程 CLI：`python -m skillvideo.run_daily [run|verify] [--dry-run] [--date YYYY-MM-DD] [--eps N,N] [--force] [--catch-up]`。

run：製作隔天 02:00、14:00 兩個 slot 的影片並預約公開（冪等，補跑不重複上傳）。
run --catch-up：只補今天尚未過、尚未排程的 slot（08:00 觸發，救當天 14:00）。
verify：檢查已過 publishAt 的集數是否真的變成 public。

防重複上傳三道防線：
1. videos.insert 一拿到 video_id 立刻寫 state=uploaded 並存檔，之後才做字幕 / 縮圖 / 播放清單；
2. 上傳後步驟全部降級為警告；上傳後存檔失敗 → 中止整個 run 並通知 video_id；
3. 曾渲染 / 上傳 / 失敗過（或 state.json 遺失）的集數，上傳前先到 YouTube 反查唯一標記。
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from . import notify, qa, render, script_gen, state, youtube
from .config import (DOTENV_PATH, MAX_ATTEMPTS, ROLLOVER_HOUR, SLOT_HOURS, TAIPEI_TZ,
                     WORK_RETENTION_DAYS, Config, ConfigError, load_config, load_dotenv_file, tail_text)

logger = logging.getLogger("skillvideo")

BLACKOUT_WINDOW = timedelta(hours=2)
# 開始製作前至少要離公開 60 分鐘（製作一集約 20～30 分鐘）
MIN_LEAD = timedelta(minutes=60)
# 真正上傳前再檢查一次：YouTube 不接受過去的 publishAt
UPLOAD_MIN_LEAD = timedelta(minutes=15)
EXIT_OK, EXIT_FAILED, EXIT_CONFIG, EXIT_LOCKED = 0, 1, 2, 3
RESULT_OK, RESULT_FAILED, RESULT_ABORT = "ok", "failed", "abort"
PIPELINE_ERROR_SUBJECT = "[影片流水線錯誤]"


class QAFailedError(Exception):
    """品質閘門未通過。"""


class SlotTooLateError(Exception):
    """製作完成時已來不及在 publishAt 前上傳。"""


class PostUploadStateError(Exception):
    """影片已上傳但 state.json 寫不進去：必須中止整個 run，避免下次重複上傳。"""

    def __init__(self, video_id: str, cause: BaseException):
        super().__init__(f"影片 {video_id} 已上傳，但 state.json 寫入失敗：{cause}")
        self.video_id = video_id


class AlreadyPublishedError(Exception):
    """反查發現本集早已公開：沿用紀錄，不再上傳。"""

    def __init__(self, video_id: str):
        super().__init__(f"本集已公開（{video_id}），不再重複上傳")
        self.video_id = video_id


# ---- 時間 / slot ----
@dataclass(frozen=True)
class Slot:
    """一個發布時段（台北時間）。"""

    publish_dt: datetime

    @property
    def publish_at(self) -> str:
        return to_rfc3339_utc(self.publish_dt)


def to_rfc3339_utc(dt: datetime) -> str:
    """aware datetime → `2026-09-26T18:00:00Z`（YouTube publishAt 格式）。"""
    if dt.tzinfo is None:
        raise ValueError("需要含時區的 datetime")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_rfc3339_utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def production_date(now: datetime) -> date:
    """製作日：06:00 前（例如 00:00 補跑）仍算前一天，讓補跑指向同一批 slot。"""
    return (now.astimezone(TAIPEI_TZ) - timedelta(hours=ROLLOVER_HOUR)).date()


def compute_slots(prod_date: date) -> list[Slot]:
    """製作日的隔天 02:00、14:00（台北）。"""
    day = prod_date + timedelta(days=1)
    return [Slot(datetime(day.year, day.month, day.day, hour, tzinfo=TAIPEI_TZ)) for hour in SLOT_HOURS]


def catch_up_slots(now: datetime) -> list[Slot]:
    """今天（台北）的兩個 slot；已過或太近的由 run_command 以 MIN_LEAD 過濾。"""
    return compute_slots(now.astimezone(TAIPEI_TZ).date() - timedelta(days=1))


# ---- 執行環境 ----
@dataclass
class RunContext:
    """一次執行所需的共用物件（測試時可整個替換）。"""

    cfg: Config
    topics: dict[int, dict[str, Any]]
    state: dict[str, Any]
    notifier: Any
    dry_run: bool = False
    now: Callable[[], datetime] = field(default=lambda: datetime.now(TAIPEI_TZ))
    service_factory: Optional[Callable[[], Any]] = None
    force: bool = False
    lookup_all: bool = False       # state.json 遺失時，每一集上傳前都要反查
    _service: Any = None

    def service(self) -> Any:
        if self._service is None:
            factory = self.service_factory or self._default_service
            self._service = factory()
        return self._service

    def _default_service(self) -> Any:
        if self.cfg.yt_token_path is None:
            raise youtube.AuthError("未設定 YT_TOKEN_PATH")
        return youtube.build_service(youtube.load_credentials(self.cfg.yt_token_path))

    def save(self) -> None:
        if not self.dry_run:
            state.save_state(self.cfg.state_path, self.state)


@dataclass
class SlotPlan:
    slot: Slot
    ep: Optional[int] = None
    done_ep: Optional[int] = None


def pinned_forced_eps(st: dict[str, Any], forced: Sequence[int]) -> dict[str, int]:
    """--force 指定的已完成集數只能排回原本的 slot：回傳 {publish_at: ep}。"""
    pinned = {}
    for ep in forced:
        item = state.entry(st, ep)
        if item.get("state") in state.DONE_STATES and item.get("publish_at"):
            pinned[item["publish_at"]] = ep
    return pinned


def plan_slots(st: dict[str, Any], slots: Sequence[Slot], forced: Sequence[int] = ()) -> list[SlotPlan]:
    """每個 slot 決定要做哪一集；已有影片的 slot 標記 done_ep（冪等跳過）。

    呼叫端必須先過濾掉太近 / 已過的 slot，否則被跳過的 slot 會先佔走最小集數造成亂序。
    --force 的已完成集數固定排回原 slot（即使該 slot 已有影片）。
    """
    pinned = pinned_forced_eps(st, forced)
    plans, chosen = [], list(pinned.values())
    queue = [ep for ep in forced if ep not in pinned.values()]
    for slot in slots:
        if slot.publish_at in pinned:
            plans.append(SlotPlan(slot, ep=pinned[slot.publish_at]))
            continue
        done = state.find_done_for_slot(st, slot.publish_at)
        if done is not None:
            plans.append(SlotPlan(slot, done_ep=done))
            continue
        ep: Optional[int] = queue.pop(0) if queue else state.next_candidate(st, exclude=chosen)
        if ep is not None:
            chosen.append(ep)
        plans.append(SlotPlan(slot, ep=ep))
    return plans


# ---- 單集製作 ----
@dataclass
class EpisodeAssets:
    bundle: script_gen.ScriptBundle
    full_title: str
    rendered: render.RenderResult
    qa_result: qa.QAResult


def episode_dir(cfg: Config, ep: int, slot: Slot) -> Path:
    return cfg.work_dir / "episodes" / f"ep{ep:03d}-{slot.publish_dt:%Y%m%d-%H%M}"


def make_assets(ctx: RunContext, ep: int, slot: Slot) -> EpisodeAssets:
    """腳本 → 渲染 → QA；QA 不過拋 QAFailedError。"""
    ep_dir = episode_dir(ctx.cfg, ep, slot)
    bundle = script_gen.generate_script(ctx.cfg, ctx.topics[ep], ep_dir)
    full_title = youtube.build_full_title(ep, bundle.script["title"])
    rendered = render.render_episode(ctx.cfg, bundle.script, ep, ep_dir)
    qa_result = qa.run_qa(rendered, bundle.script, full_title, state.previous_titles(ctx.state, ep),
                          bundle.sensitive, [s.demo_text for s in bundle.sources], ep_dir / "qa")
    if not qa_result.passed:
        raise QAFailedError(qa_result.summary())
    return EpisodeAssets(bundle, full_title, rendered, qa_result)


def _save_after_upload(ctx: RunContext, video_id: str) -> None:
    try:
        ctx.save()
    except state.StateError as exc:
        raise PostUploadStateError(video_id, exc) from exc


def _reuse_existing(ctx: RunContext, service: Any, ep: int, slot: Slot) -> Optional[str]:
    """到 YouTube 反查本集是否早已上傳；找到就沿用（必要時改排 publishAt），不重傳。"""
    existing = youtube.find_uploaded_video(service, ep)
    if existing is None:
        return None
    if existing.privacy_status == "public":
        state.mark_published(ctx.state, ep, existing.video_id)
        _save_after_upload(ctx, existing.video_id)
        raise AlreadyPublishedError(existing.video_id)
    if existing.publish_at != slot.publish_at:
        youtube.reschedule_video(service, existing.video_id, slot.publish_at)
    logger.warning("第 %d 集在 YouTube 已有影片 %s，沿用不重傳", ep, existing.video_id)
    return existing.video_id


def _ensure_upload_lead(ctx: RunContext, slot: Slot) -> None:
    if slot.publish_dt - ctx.now() < UPLOAD_MIN_LEAD:
        raise SlotTooLateError(f"距 {notify.format_taipei(slot.publish_dt)} 不到 15 分鐘，放棄上傳")


def _upload_or_reuse(ctx: RunContext, service: Any, ep: int, slot: Slot, assets: EpisodeAssets,
                     needs_lookup: bool) -> tuple[str, bool]:
    """回傳（video_id, 是否沿用既有影片）。"""
    video_id = _reuse_existing(ctx, service, ep, slot) if needs_lookup else None
    if video_id is not None:
        return video_id, True
    script = assets.bundle.script
    body = youtube.build_video_body(assets.full_title, script["description"],
                                    script.get("tags") or [], slot.publish_at, ep)
    return youtube.upload_video(service, assets.rendered.video_path, body), False


def publish_assets(ctx: RunContext, ep: int, slot: Slot, assets: EpisodeAssets) -> tuple[str, bool]:
    """上傳（或沿用既有影片）→ 立刻寫 uploaded → 上傳後步驟 → scheduled；回傳（video_id, 是否沿用）。"""
    script, rendered = assets.bundle.script, assets.rendered
    item = state.entry(ctx.state, ep)
    needs_lookup = not ctx.force and (ctx.lookup_all or state.needs_youtube_lookup(item))
    state.mark_rendered(ctx.state, ep, script["title"])
    ctx.save()
    _ensure_upload_lead(ctx, slot)
    service = ctx.service()
    video_id, reused = _upload_or_reuse(ctx, service, ep, slot, assets, needs_lookup)
    state.mark_uploaded(ctx.state, ep, video_id, slot.publish_at, script["title"])
    _save_after_upload(ctx, video_id)
    # 沿用的影片可能早就在播放清單裡，重複加入會出現兩次
    playlist_id = youtube.finish_upload(service, video_id, rendered.srt_path, rendered.thumb_path,
                                        ctx.cfg.yt_playlist_id or ctx.state.get("playlist_id"),
                                        check_playlist=reused)
    if playlist_id and not ctx.cfg.yt_playlist_id:
        ctx.state["playlist_id"] = playlist_id
    state.mark_scheduled(ctx.state, ep, video_id, slot.publish_at, script["title"])
    _save_after_upload(ctx, video_id)
    render.cleanup_intermediates(rendered.video_path.parent)
    return video_id, reused


def produce_episode(ctx: RunContext, ep: int, slot: Slot) -> None:
    """完整處理一集；dry-run 做到 QA 為止只印結果。"""
    logger.info("開始製作第 %d 集（slot %s）", ep, slot.publish_at)
    assets = make_assets(ctx, ep, slot)
    if ctx.dry_run:
        print(f"[dry-run] 第 {ep} 集完成到 QA：{assets.full_title}\n"
              f"  影片：{assets.rendered.video_path}\n  {assets.qa_result.summary()}")
        return
    video_id, reused = publish_assets(ctx, ep, slot, assets)
    subject, text = notify.format_success(ep, assets.full_title, youtube.video_url(video_id),
                                          slot.publish_dt, assets.rendered.duration,
                                          assets.qa_result.summary(), reused=reused)
    ctx.notifier.send(subject, text, [assets.rendered.thumb_path, *assets.qa_result.screenshots])


def handle_failure(ctx: RunContext, ep: int, slot: Slot, exc: BaseException, count_attempt: bool = True) -> None:
    """記錄 failed（dry-run 不寫 state）並通知；距公開不到 2 小時標示開天窗。"""
    error_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    item = state.entry(ctx.state, ep)
    attempts = int(item.get("attempts") or 0)
    # 已有影片（uploaded / scheduled / published）時不可改回 failed，否則下次會被重新排片上傳
    if not ctx.dry_run and item.get("state") not in state.DONE_STATES:
        attempts = state.mark_failed(ctx.state, ep, f"{type(exc).__name__}: {exc}", count_attempt)
        try:
            ctx.save()
        except state.StateError as save_exc:
            logger.error("失敗狀態寫入 state.json 失敗：%s", save_exc)
    blackout = slot.publish_dt - ctx.now() < BLACKOUT_WINDOW
    subject, text = notify.format_failure(ep, slot.publish_dt, error_text, attempts,
                                          attempts >= MAX_ATTEMPTS, blackout)
    ctx.notifier.send(subject, text)


def _notify_post_upload_state_error(ctx: RunContext, ep: int, exc: PostUploadStateError) -> None:
    logger.critical("第 %d 集：%s", ep, exc)
    ctx.notifier.send(f"{PIPELINE_ERROR_SUBJECT} 第 {ep} 集已上傳但 state.json 無法寫入，請人工處理",
                      f"video_id：{exc.video_id}\n連結：{youtube.video_url(exc.video_id)}\n"
                      f"請手動把 state.json 第 {ep} 集改成 scheduled 並填入 video_id，"
                      f"否則下次執行會靠 YouTube 反查沿用。\n錯誤：{exc}")


def process_slot(ctx: RunContext, ep: int, slot: Slot) -> str:
    """處理一個 slot；任何例外都轉成 failed + 通知，不讓程序靜默結束。"""
    try:
        produce_episode(ctx, ep, slot)
        return RESULT_OK
    except PostUploadStateError as exc:
        _notify_post_upload_state_error(ctx, ep, exc)
        return RESULT_ABORT
    except AlreadyPublishedError as exc:
        ctx.notifier.send(f"[影片流水線] 第 {ep} 集早已公開，本 slot 未排入", str(exc))
        return RESULT_FAILED
    except (youtube.QuotaExceededError, youtube.AuthError) as exc:
        # 配額 / 授權不是本集內容的問題，不累計 attempts，並停止後續上傳
        logger.error("第 %d 集因 %s 中止：%s", ep, type(exc).__name__, exc)
        handle_failure(ctx, ep, slot, exc, count_attempt=False)
        return RESULT_ABORT
    except SlotTooLateError as exc:
        handle_failure(ctx, ep, slot, exc, count_attempt=False)
        return RESULT_FAILED
    except Exception as exc:  # noqa: BLE001 — SPEC §12：未預期例外也必須記錄並通知
        logger.exception("第 %d 集製作失敗", ep)
        handle_failure(ctx, ep, slot, exc)
        return RESULT_FAILED


def handle_no_candidate(ctx: RunContext, slot: Slot) -> bool:
    """沒有可製作的集數：全部做完只通知一次「系列完結」（回傳 False）；有暫停集數則每次提醒（回傳 True）。"""
    paused = state.paused_episodes(ctx.state)
    if not paused:
        if ctx.state.get("series_complete_notified"):
            logger.info("系列已完結，slot %s 不再排片", slot.publish_at)
            return False
        ctx.notifier.send("[影片流水線] 系列完結", "73 集已全部排程或公開，之後的 slot 不再製作。")
        ctx.state["series_complete_notified"] = True
        ctx.save()
        return False
    subject, text = notify.format_failure(None, slot.publish_dt,
                                          f"沒有可製作的集數；暫停中需人工處理：{paused}",
                                          0, False, slot.publish_dt - ctx.now() < BLACKOUT_WINDOW)
    ctx.notifier.send(subject, text)
    return True


def check_youtube_auth(ctx: RunContext) -> bool:
    """正式執行前先確認 YouTube 認證可用，避免做完影片才發現傳不上去。"""
    try:
        ctx.service()
        return True
    except Exception as exc:  # noqa: BLE001 — 任何認證 / 建立 service 的錯誤都要通知
        logger.error("YouTube 認證檢查失敗：%s", exc)
        ctx.notifier.send(f"{PIPELINE_ERROR_SUBJECT} YouTube 認證失敗，本次不製作",
                          f"{type(exc).__name__}: {exc}\n可能需要重新授權 YouTube token。")
        return False


def usable_slots(ctx: RunContext, slots: Sequence[Slot]) -> list[Slot]:
    """過濾掉已過或距公開不足 60 分鐘的 slot（dry-run 不過濾，方便隨時試做）。"""
    if ctx.dry_run:
        return list(slots)
    usable = []
    for slot in slots:
        if slot.publish_dt - ctx.now() < MIN_LEAD:
            logger.warning("slot %s 已過或不足 60 分鐘，無法再製作", slot.publish_at)
        else:
            usable.append(slot)
    return usable


def run_command(ctx: RunContext, slots: Sequence[Slot], forced: Sequence[int] = ()) -> int:
    """依 slot 逐一製作；回傳結束碼。

    必須先過濾 slot 再分配集數：否則被跳過的 slot 會先佔走最小集數，造成集數亂序。
    """
    if not ctx.dry_run and not check_youtube_auth(ctx):
        return EXIT_FAILED
    failed = False
    for plan in plan_slots(ctx.state, usable_slots(ctx, slots), forced):
        if plan.done_ep is not None:
            logger.info("slot %s 已由第 %d 集排定，跳過", plan.slot.publish_at, plan.done_ep)
            continue
        if plan.ep is None:
            failed = handle_no_candidate(ctx, plan.slot) or failed
            continue
        result = process_slot(ctx, plan.ep, plan.slot)
        failed = failed or result != RESULT_OK
        if result == RESULT_ABORT:
            break
    return EXIT_FAILED if failed else EXIT_OK


# ---- verify ----
VERIFY_STATES = ("scheduled", "uploaded")


def due_episodes(st: dict[str, Any], now: datetime) -> list[tuple[int, dict[str, Any]]]:
    """已過 publishAt 但仍是 scheduled / uploaded 的集數。"""
    due = []
    for key, item in st["episodes"].items():
        if item.get("state") not in VERIFY_STATES or not item.get("publish_at") or not item.get("video_id"):
            continue
        if parse_rfc3339_utc(item["publish_at"]) <= now:
            due.append((int(key), item))
    return sorted(due, key=lambda pair: pair[0])


def _verify_one(ctx: RunContext, ep: int, item: dict[str, Any]) -> tuple[bool, str]:
    url = youtube.video_url(item["video_id"])
    try:
        status = youtube.get_privacy_status(ctx.service(), item["video_id"])
    except Exception as exc:  # noqa: BLE001 — 單支查詢失敗不影響其他集數
        return False, f"#{ep:03d} {url} 查詢失敗：{type(exc).__name__}: {exc}"
    if status == "public":
        state.mark_published(ctx.state, ep)
        return True, f"#{ep:03d} {url} 已公開"
    return False, f"#{ep:03d} {url} privacyStatus={status}"


def verify_command(ctx: RunContext) -> int:
    """逐支確認 privacyStatus == public，最後統一存檔；異常立即通知。"""
    due = due_episodes(ctx.state, ctx.now())
    if not due:
        logger.info("沒有需要驗證的集數")
        return EXIT_OK
    ok_lines, problems = [], []
    for ep, item in due:
        ok, line = _verify_one(ctx, ep, item)
        (ok_lines if ok else problems).append(line)
    try:
        ctx.save()
    except state.StateError as exc:
        problems.append(f"state.json 寫入失敗：{exc}")
    if problems:
        text = "以下影片過了預定公開時間仍未確認公開（可能被鎖成私人）：\n" + "\n".join(problems)
        ctx.notifier.send("[警告] 公開後驗證失敗", text)
        return EXIT_FAILED
    ctx.notifier.send(f"[公開驗證] {len(ok_lines)} 集已公開", "\n".join(ok_lines))
    return EXIT_OK


# ---- CLI ----
def parse_eps(text: Optional[str]) -> list[int]:
    """`--eps 3,4` → [3, 4]。"""
    if not text:
        return []
    try:
        eps = [int(part) for part in text.split(",") if part.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--eps 格式錯誤：{text}") from exc
    if any(ep <= 0 for ep in eps):
        raise argparse.ArgumentTypeError("--eps 集數必須是正整數")
    return eps


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m skillvideo.run_daily",
                                     description="Claude Code Skill 技術探討：每日自動影片")
    parser.add_argument("command", nargs="?", choices=("run", "verify"), default="run")
    parser.add_argument("--dry-run", action="store_true", help="做到 QA 為止，不上傳、不通知、不寫 state")
    parser.add_argument("--date", type=date.fromisoformat, help="製作日 YYYY-MM-DD（slot 為隔天 02:00、14:00）")
    parser.add_argument("--eps", type=parse_eps, default=[], help="指定集數，依序對應 slot，例如 3,4")
    parser.add_argument("--force", action="store_true", help="允許 --eps 指定已上傳 / 排程 / 公開的集數並重新上傳")
    parser.add_argument("--catch-up", action="store_true", help="只補今天尚未過、尚未排程的 slot")
    return parser


def setup_logging(work_dir: Path, today: date) -> Path:
    """寫 WORK_DIR/logs/YYYY-MM-DD.log（UTF-8）並同時輸出到 stderr。"""
    log_dir = work_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{today.isoformat()}.log"
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()):
        handler.setFormatter(fmt)
        root.addHandler(handler)
    return log_path


def cleanup_work_dir(work_dir: Path, days: int = WORK_RETENTION_DAYS, now: Optional[float] = None) -> int:
    """刪除超過保留天數的集數產物與 log，回傳刪除數量。"""
    cutoff = (now if now is not None else time.time()) - days * 86400
    removed = 0
    targets = list((work_dir / "episodes").glob("*")) + list((work_dir / "logs").glob("*.log"))
    for path in targets:
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(path) if path.is_dir() else path.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("清理 %s 失敗：%s", path, exc)
    return removed


def _load_context(cfg: Config, args: argparse.Namespace, notifier: Any) -> RunContext:
    lookup_all = state.state_file_missing(cfg.state_path)
    topics = state.load_topics(cfg.topics_path)
    st = state.load_state(cfg.state_path, topics)
    return RunContext(cfg=cfg, topics={int(t["ep"]): t for t in topics}, state=st, notifier=notifier,
                      dry_run=args.dry_run, force=args.force, lookup_all=lookup_all)


def validate_forced_eps(ctx: RunContext, eps: Sequence[int], force: bool, slots: Sequence[Slot]) -> Optional[str]:
    """回傳錯誤訊息：不存在的集數、未加 --force 卻指定已完成集數、或 --force 集數的原 slot 不在本次範圍。"""
    unknown = [ep for ep in eps if ep not in ctx.topics]
    if unknown:
        return f"topics.json 沒有這些集數：{unknown}"
    done = [ep for ep in eps if state.entry(ctx.state, ep).get("state") in state.DONE_STATES]
    if done and not force:
        return f"這些集數已上傳 / 排程 / 公開：{done}；確定要重做請加 --force"
    targets = {s.publish_at for s in slots}
    for ep in done:
        original = state.entry(ctx.state, ep).get("publish_at")
        # 排到別的 slot 會讓原 slot 與新 slot 各有一支同集影片
        if original and original not in targets:
            return f"--force 的第 {ep} 集只能排回原本的 slot {original}，請用 --date 指定對應的製作日"
    return None


def warn_forced_reupload(ctx: RunContext, eps: Sequence[int]) -> None:
    """--force 重傳前提醒使用者處理舊影片（程式不會自動刪除 YouTube 上的影片）。"""
    for ep in eps:
        old_id = state.entry(ctx.state, ep).get("video_id")
        if not old_id:
            continue
        logger.warning("--force 重傳第 %d 集，舊影片 %s 仍在 YouTube 上", ep, old_id)
        ctx.notifier.send(f"[影片流水線] --force 重傳第 {ep} 集，請處理舊影片",
                          f"舊 video_id：{old_id}\n連結：{youtube.video_url(old_id)}\n"
                          "請到 YouTube Studio 手動刪除或改為私人，避免同一集出現兩支影片。")


def _dispatch(args: argparse.Namespace, cfg: Config, notifier: Any) -> int:
    ctx = _load_context(cfg, args, notifier)
    if args.command == "verify":
        return verify_command(ctx)
    if args.catch_up:
        slots = catch_up_slots(ctx.now())
    else:
        slots = compute_slots(args.date or production_date(ctx.now()))
    error = validate_forced_eps(ctx, args.eps, args.force, slots)
    if error:
        logger.error(error)
        print(f"參數錯誤：{error}", file=sys.stderr)
        return EXIT_CONFIG
    if args.force:
        warn_forced_reupload(ctx, args.eps)
    cleanup_work_dir(cfg.work_dir)
    return run_command(ctx, slots, args.eps)


def _run_locked(args: argparse.Namespace, cfg: Config, notifier: Any) -> int:
    """持有 OS 級鎖執行；鎖被占用超過 30 分鐘發一次通知。"""
    marker = cfg.work_dir / "run.lock.busy"
    try:
        with state.FileLock(cfg.work_dir / "run.lock"):
            state.clear_lock_busy(marker)
            return _dispatch(args, cfg, notifier)
    except state.LockBusyError as exc:
        busy_seconds, should_alert = state.record_lock_busy(marker)
        logger.warning("%s（已持續 %.0f 分鐘），本次不執行", exc, busy_seconds / 60)
        if should_alert:
            notifier.send(f"{PIPELINE_ERROR_SUBJECT} 執行鎖被占用超過 30 分鐘",
                          f"{exc}\n已持續 {busy_seconds / 60:.0f} 分鐘，可能有卡住的程序，請檢查。")
        return EXIT_LOCKED


def _load_cfg_or_notify(args: argparse.Namespace) -> Optional[Config]:
    is_run = args.command == "run"
    try:
        load_dotenv_file(DOTENV_PATH, os.environ)
        return load_config(dry_run=args.dry_run, detect_tools=is_run, need_kindle=is_run)
    except ConfigError as exc:
        print(f"設定錯誤：{exc}", file=sys.stderr)
        if not args.dry_run:
            # Config 起不來就沒有 Notifier 可用，直接拿環境變數的通知憑證，避免靜默失敗
            notify.emergency_notify(f"{PIPELINE_ERROR_SUBJECT} 設定載入失敗（{args.command}）", str(exc))
        return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = _load_cfg_or_notify(args)
    if cfg is None:
        return EXIT_CONFIG
    setup_logging(cfg.work_dir, datetime.now(TAIPEI_TZ).date())
    notifier = notify.Notifier(cfg, dry_run=args.dry_run)
    try:
        return _run_locked(args, cfg, notifier)
    except Exception as exc:  # noqa: BLE001 — 最外層防線：任何例外都要留 traceback 並通知
        logger.exception("執行失敗（%s）", args.command)
        detail = tail_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), 20)
        notifier.send(f"{PIPELINE_ERROR_SUBJECT} {args.command}", detail)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
