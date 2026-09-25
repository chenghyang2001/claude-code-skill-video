"""run_daily：slot 時間換算、冪等、重試上限、dry-run、失敗通知、防重複上傳三道防線、
配額中止、認證預檢、系列完結、--eps/--force、catch-up、verify、鎖忙碌通知、最外層防線。"""
from __future__ import annotations

import os
import time
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from conftest import make_script, make_topics
from skillvideo import notify, qa, run_daily, state, youtube
from skillvideo.config import TAIPEI_TZ
from skillvideo.render import RenderResult
from skillvideo.script_gen import ScriptBundle

EVENING = datetime(2026, 9, 26, 20, 0, tzinfo=TAIPEI_TZ)
SLOTS = run_daily.compute_slots(date(2026, 9, 26))


def make_ctx(cfg, notifier, now=EVENING, dry_run=False, **kwargs):
    topics = make_topics(4)
    return run_daily.RunContext(cfg=cfg, topics={t["ep"]: t for t in topics}, state=state.init_state(topics),
                                notifier=notifier, dry_run=dry_run, now=lambda: now,
                                service_factory=lambda: object(), **kwargs)


def fake_assets(tmp_path, ep=1):
    ep_dir = tmp_path / f"ep{ep}"
    ep_dir.mkdir(exist_ok=True)
    script = make_script(title=f"skill{ep}：重點")
    rendered = RenderResult(ep_dir / "v.mp4", ep_dir / "v.srt", ep_dir / "t.jpg", 300.0, "+8%")
    return run_daily.EpisodeAssets(ScriptBundle(script), f"系列 #{ep:03d}｜skill{ep}：重點", rendered,
                                   qa.QAResult(passed=True, meta={"duration": 300.0}))


@pytest.fixture
def fake_youtube(monkeypatch, tmp_path):
    """記錄上傳動作的假 YouTube；可改 find_result / finish_error 模擬各種情境。"""
    log = SimpleNamespace(uploads=[], finished=[], lookups=[], reschedules=[], playlist_checks=[],
                          find_result=None, finish_error=None, state_at_finish=[])

    def upload(service, video, body):
        log.uploads.append(body["status"]["publishAt"])
        return f"VID{len(log.uploads)}"

    def finish(service, video_id, srt, thumb, playlist_id, check_playlist=False):
        log.finished.append(video_id)
        log.playlist_checks.append(check_playlist)
        if log.finish_error:
            raise log.finish_error
        return "PL1"

    def find(service, ep):
        log.lookups.append(ep)
        return log.find_result

    monkeypatch.setattr(youtube, "upload_video", upload)
    monkeypatch.setattr(youtube, "finish_upload", finish)
    monkeypatch.setattr(youtube, "find_uploaded_video", find)
    monkeypatch.setattr(youtube, "reschedule_video", lambda s, vid, at: log.reschedules.append((vid, at)))
    return log


# ---- 時間 / 計畫 ----
def test_slots_cross_day_and_utc():
    assert [s.publish_at for s in SLOTS] == ["2026-09-26T18:00:00Z", "2026-09-27T06:00:00Z"]
    year_end = run_daily.compute_slots(date(2026, 12, 31))
    assert year_end[0].publish_dt.date() == date(2027, 1, 1)
    assert year_end[0].publish_at == "2026-12-31T18:00:00Z"


def test_production_date_rolls_back_before_six():
    assert run_daily.production_date(EVENING) == date(2026, 9, 26)
    midnight = datetime(2026, 9, 27, 0, 0, tzinfo=TAIPEI_TZ)
    assert run_daily.production_date(midnight) == date(2026, 9, 26)
    with pytest.raises(ValueError):
        run_daily.to_rfc3339_utc(datetime(2026, 9, 26, 2, 0))


def test_catch_up_slots_are_today():
    morning = datetime(2026, 9, 27, 8, 0, tzinfo=TAIPEI_TZ)
    assert run_daily.catch_up_slots(morning) == SLOTS


def test_catch_up_only_processes_remaining_slot(cfg, notifier, monkeypatch):
    morning = datetime(2026, 9, 27, 8, 0, tzinfo=TAIPEI_TZ)
    ctx = make_ctx(cfg, notifier, now=morning)
    produced = []
    monkeypatch.setattr(run_daily, "produce_episode",
                        lambda c, ep, slot: produced.append((ep, slot.publish_dt.hour)))
    run_daily.run_command(ctx, run_daily.catch_up_slots(morning))
    # 重現：舊版 02:00 雖被跳過卻先佔走 ep1，14:00 變成 ep2
    assert produced == [(1, 14)]


def test_boot_rerun_skipping_0200_still_gives_1400_smallest_ep(cfg, notifier, monkeypatch):
    """前一晚開機補跑（Persistent timer）在 01:30 觸發：02:00 太近被跳過，14:00 仍要拿最小集數。"""
    ctx = make_ctx(cfg, notifier, now=datetime(2026, 9, 27, 1, 30, tzinfo=TAIPEI_TZ))
    ctx.state["episodes"]["1"].update(state="failed", attempts=1)
    produced = []
    monkeypatch.setattr(run_daily, "produce_episode",
                        lambda c, ep, slot: produced.append((ep, slot.publish_dt.hour)))
    run_daily.run_command(ctx, SLOTS)
    assert produced == [(1, 14)]


def test_idempotent_skip_of_scheduled_slot(cfg, notifier, monkeypatch):
    ctx = make_ctx(cfg, notifier)
    state.mark_scheduled(ctx.state, 1, "VID1", SLOTS[0].publish_at, "skill1：重點")
    produced = []
    monkeypatch.setattr(run_daily, "produce_episode", lambda c, ep, slot: produced.append((ep, slot.publish_at)))
    assert run_daily.run_command(ctx, SLOTS) == run_daily.EXIT_OK
    assert produced == [(2, SLOTS[1].publish_at)]


def test_failed_episode_over_limit_is_skipped_in_plan():
    st = state.init_state(make_topics(4))
    st["episodes"]["1"].update(state="failed", attempts=3)
    assert [p.ep for p in run_daily.plan_slots(st, SLOTS)] == [2, 3]


def test_forced_eps_map_to_slots_in_order():
    st = state.init_state(make_topics(4))
    assert [p.ep for p in run_daily.plan_slots(st, SLOTS, forced=[4, 3])] == [4, 3]


def test_forced_done_episode_requires_force(cfg, notifier):
    ctx = make_ctx(cfg, notifier)
    state.mark_scheduled(ctx.state, 2, "V", SLOTS[0].publish_at, "t")
    assert "--force" in run_daily.validate_forced_eps(ctx, [2], False, SLOTS)
    assert run_daily.validate_forced_eps(ctx, [2], True, SLOTS) is None
    assert "沒有這些集數" in run_daily.validate_forced_eps(ctx, [99], True, SLOTS)


def test_force_only_allowed_back_to_original_slot(cfg, notifier):
    ctx = make_ctx(cfg, notifier)
    state.mark_scheduled(ctx.state, 2, "V", SLOTS[0].publish_at, "t")
    other_day = run_daily.compute_slots(date(2026, 9, 27))
    error = run_daily.validate_forced_eps(ctx, [2], True, other_day)
    assert error is not None and "原本的 slot" in error


def test_force_pins_episode_to_its_original_slot():
    st = state.init_state(make_topics(4))
    state.mark_scheduled(st, 1, "V", SLOTS[1].publish_at, "t")
    plans = run_daily.plan_slots(st, SLOTS, forced=[1])
    assert [(p.ep, p.done_ep) for p in plans] == [(2, None), (1, None)]


def test_force_warns_about_old_video(cfg, notifier):
    ctx = make_ctx(cfg, notifier)
    state.mark_scheduled(ctx.state, 2, "OLDVID", SLOTS[0].publish_at, "t")
    run_daily.warn_forced_reupload(ctx, [2, 3])
    assert len(notifier.messages) == 1
    subject, text, _ = notifier.messages[0]
    assert "第 2 集" in subject and "OLDVID" in text and "youtu.be/OLDVID" in text


# ---- 正常 / dry-run ----
def test_dry_run_does_not_upload_notify_or_write_state(cfg, notifier, monkeypatch, tmp_path, capsys):
    ctx = make_ctx(cfg, notifier, dry_run=True)
    monkeypatch.setattr(run_daily, "make_assets", lambda c, ep, slot: fake_assets(tmp_path, ep))

    def must_not_call(*a, **k):
        raise AssertionError("dry-run 不應碰 YouTube")

    monkeypatch.setattr(youtube, "upload_video", must_not_call)
    monkeypatch.setattr(youtube, "build_service", must_not_call)
    ctx.service_factory = must_not_call
    assert run_daily.run_command(ctx, SLOTS) == run_daily.EXIT_OK
    assert notifier.messages == []
    assert not cfg.state_path.exists()
    assert "dry-run" in capsys.readouterr().out


def test_success_updates_state_and_rerun_does_not_reupload(cfg, notifier, monkeypatch, tmp_path, fake_youtube):
    ctx = make_ctx(cfg, notifier)
    monkeypatch.setattr(run_daily, "make_assets", lambda c, ep, slot: fake_assets(tmp_path, ep))
    assert run_daily.run_command(ctx, SLOTS) == run_daily.EXIT_OK
    saved = state.load_state(cfg.state_path, make_topics(4))
    assert saved["episodes"]["1"]["state"] == "scheduled" and saved["episodes"]["2"]["video_id"] == "VID2"
    assert saved["playlist_id"] == "PL1"
    assert fake_youtube.uploads == [s.publish_at for s in SLOTS]
    assert fake_youtube.lookups == []                    # 全新集數不需反查
    assert len(notifier.messages) == 2 and "已排程" in notifier.messages[0][0]
    assert run_daily.run_command(ctx, SLOTS) == run_daily.EXIT_OK
    assert len(fake_youtube.uploads) == 2


# ---- MUST_FIX 1：防重複上傳 ----
def test_uploaded_state_saved_before_post_upload_steps(cfg, notifier, monkeypatch, tmp_path, fake_youtube):
    """重現：舊版在字幕 / 播放清單之後才寫 state，中途當機下次會重傳。"""
    ctx = make_ctx(cfg, notifier)
    monkeypatch.setattr(run_daily, "make_assets", lambda c, ep, slot: fake_assets(tmp_path, ep))
    real_finish = youtube.finish_upload

    def finish_and_inspect(service, video_id, srt, thumb, playlist_id, check_playlist=False):
        on_disk = state.load_state(cfg.state_path, make_topics(4))
        fake_youtube.state_at_finish.append(on_disk["episodes"]["1"]["state"])
        return real_finish(service, video_id, srt, thumb, playlist_id, check_playlist)

    monkeypatch.setattr(youtube, "finish_upload", finish_and_inspect)
    run_daily.process_slot(ctx, 1, SLOTS[0])
    assert fake_youtube.state_at_finish == ["uploaded"]


def test_post_upload_crash_then_rerun_skips_slot(cfg, notifier, monkeypatch, tmp_path, fake_youtube):
    ctx = make_ctx(cfg, notifier)
    monkeypatch.setattr(run_daily, "make_assets", lambda c, ep, slot: fake_assets(tmp_path, ep))
    # SystemExit 模擬行程在上傳後被砍（不會經過 except Exception）
    fake_youtube.finish_error = SystemExit("模擬上傳後當機")
    with pytest.raises(SystemExit):
        run_daily.process_slot(ctx, 1, SLOTS[0])
    reloaded = state.load_state(cfg.state_path, make_topics(4))
    assert reloaded["episodes"]["1"]["video_id"] == "VID1"
    assert reloaded["episodes"]["1"]["state"] == "uploaded"
    ctx2 = make_ctx(cfg, notifier)
    ctx2.state = reloaded
    produced = []
    monkeypatch.setattr(run_daily, "produce_episode", lambda c, ep, slot: produced.append(slot.publish_at))
    run_daily.run_command(ctx2, SLOTS[:1])
    assert produced == [] and len(fake_youtube.uploads) == 1


def test_save_failure_after_upload_aborts_run_with_video_id(cfg, notifier, monkeypatch, tmp_path, fake_youtube):
    ctx = make_ctx(cfg, notifier)
    monkeypatch.setattr(run_daily, "make_assets", lambda c, ep, slot: fake_assets(tmp_path, ep))
    real_save = state.save_state

    def save_fails_once_uploaded(path, st):
        if any(e.get("state") == "uploaded" for e in st["episodes"].values()):
            raise state.StateError("disk full")
        real_save(path, st)

    monkeypatch.setattr(state, "save_state", save_fails_once_uploaded)
    assert run_daily.run_command(ctx, SLOTS) == run_daily.EXIT_FAILED
    assert fake_youtube.uploads == [SLOTS[0].publish_at]     # 第二個 slot 不再上傳
    subject, text, _ = notifier.messages[-1]
    assert "人工處理" in subject and "VID1" in text
    assert ctx.state["episodes"]["1"]["attempts"] == 0


def test_previously_failed_episode_reuses_existing_video(cfg, notifier, monkeypatch, tmp_path, fake_youtube):
    ctx = make_ctx(cfg, notifier)
    ctx.state["episodes"]["1"].update(state="failed", attempts=1, last_error="上次上傳後逾時")
    monkeypatch.setattr(run_daily, "make_assets", lambda c, ep, slot: fake_assets(tmp_path, ep))
    fake_youtube.find_result = youtube.ExistingVideo("OLD1", "private", "2026-09-20T18:00:00Z")
    assert run_daily.process_slot(ctx, 1, SLOTS[0]) == run_daily.RESULT_OK
    assert fake_youtube.uploads == []
    assert fake_youtube.reschedules == [("OLD1", SLOTS[0].publish_at)]
    assert ctx.state["episodes"]["1"]["video_id"] == "OLD1"
    assert ctx.state["episodes"]["1"]["state"] == "scheduled"
    assert fake_youtube.playlist_checks == [True]            # 沿用時要先查是否已在清單
    assert "沿用既有影片" in notifier.messages[-1][0]


def test_lost_state_triggers_lookup_for_fresh_episode(cfg, notifier, monkeypatch, tmp_path, fake_youtube):
    ctx = make_ctx(cfg, notifier, lookup_all=True)
    monkeypatch.setattr(run_daily, "make_assets", lambda c, ep, slot: fake_assets(tmp_path, ep))
    fake_youtube.find_result = youtube.ExistingVideo("OLD1", "private", SLOTS[0].publish_at)
    run_daily.process_slot(ctx, 1, SLOTS[0])
    assert fake_youtube.lookups == [1] and fake_youtube.uploads == [] and fake_youtube.reschedules == []


def test_load_context_detects_missing_state(cfg, notifier):
    args = SimpleNamespace(dry_run=False, force=False)
    ctx = run_daily._load_context(cfg, args, notifier)
    assert ctx.lookup_all is True
    state.save_state(cfg.state_path, ctx.state)
    assert run_daily._load_context(cfg, args, notifier).lookup_all is False
    # 只剩 .bak（比主檔落後一次存檔）時也要全面反查
    state.save_state(cfg.state_path, ctx.state)
    cfg.state_path.unlink()
    assert state.backup_path(cfg.state_path).exists()
    assert run_daily._load_context(cfg, args, notifier).lookup_all is True


def test_already_public_episode_is_recorded_not_reuploaded(cfg, notifier, monkeypatch, tmp_path, fake_youtube):
    ctx = make_ctx(cfg, notifier)
    ctx.state["episodes"]["1"].update(state="rendered")
    monkeypatch.setattr(run_daily, "make_assets", lambda c, ep, slot: fake_assets(tmp_path, ep))
    fake_youtube.find_result = youtube.ExistingVideo("PUB1", "public", "2026-09-20T18:00:00Z")
    assert run_daily.process_slot(ctx, 1, SLOTS[0]) == run_daily.RESULT_FAILED
    assert ctx.state["episodes"]["1"]["state"] == "published"
    assert fake_youtube.uploads == []


def test_force_skips_lookup(cfg, notifier, monkeypatch, tmp_path, fake_youtube):
    ctx = make_ctx(cfg, notifier, force=True)
    ctx.state["episodes"]["1"].update(state="failed", attempts=1)
    monkeypatch.setattr(run_daily, "make_assets", lambda c, ep, slot: fake_assets(tmp_path, ep))
    run_daily.process_slot(ctx, 1, SLOTS[0])
    assert fake_youtube.lookups == [] and len(fake_youtube.uploads) == 1


# ---- 時間門檻 / 失敗 ----
def test_upload_skipped_when_under_15_minutes(cfg, notifier, monkeypatch, tmp_path, fake_youtube):
    clock = {"now": EVENING}
    ctx = make_ctx(cfg, notifier)
    ctx.now = lambda: clock["now"]

    def slow_assets(c, ep, slot):
        clock["now"] = datetime(2026, 9, 27, 1, 50, tzinfo=TAIPEI_TZ)   # 做完只剩 10 分鐘
        return fake_assets(tmp_path, ep)

    monkeypatch.setattr(run_daily, "make_assets", slow_assets)
    assert run_daily.process_slot(ctx, 1, SLOTS[0]) == run_daily.RESULT_FAILED
    assert fake_youtube.uploads == []
    assert ctx.state["episodes"]["1"]["attempts"] == 0


def test_slot_within_60_minutes_is_not_started(cfg, notifier, monkeypatch):
    ctx = make_ctx(cfg, notifier, now=datetime(2026, 9, 27, 1, 10, tzinfo=TAIPEI_TZ))
    produced = []
    monkeypatch.setattr(run_daily, "produce_episode",
                        lambda c, ep, slot: produced.append((ep, slot.publish_dt.hour)))
    run_daily.run_command(ctx, SLOTS)
    assert produced == [(1, 14)]


def test_failure_marks_failed_and_flags_blackout(cfg, notifier, monkeypatch):
    ctx = make_ctx(cfg, notifier, now=datetime(2026, 9, 27, 0, 30, tzinfo=TAIPEI_TZ))

    def boom(c, ep, slot):
        raise RuntimeError("ffmpeg 爆了")

    monkeypatch.setattr(run_daily, "make_assets", boom)
    assert run_daily.run_command(ctx, SLOTS) == run_daily.EXIT_FAILED
    assert ctx.state["episodes"]["1"]["state"] == "failed"
    assert ctx.state["episodes"]["1"]["attempts"] == 1
    subjects = [m[0] for m in notifier.messages]
    assert "開天窗" in subjects[0] and "開天窗" not in subjects[1]
    assert "ffmpeg 爆了" in notifier.messages[0][1]


def test_third_failure_pauses_episode(cfg, notifier, monkeypatch):
    ctx = make_ctx(cfg, notifier)
    ctx.state["episodes"]["1"].update(state="failed", attempts=2)
    monkeypatch.setattr(run_daily, "make_assets", lambda c, ep, slot: (_ for _ in ()).throw(ValueError("x")))
    assert run_daily.process_slot(ctx, 1, SLOTS[0]) == run_daily.RESULT_FAILED
    assert ctx.state["episodes"]["1"]["attempts"] == 3
    assert "暫停" in notifier.messages[-1][1]
    assert state.next_candidate(ctx.state) == 2


def test_quota_error_aborts_without_counting_attempt(cfg, notifier, monkeypatch, tmp_path, fake_youtube):
    ctx = make_ctx(cfg, notifier)
    monkeypatch.setattr(run_daily, "make_assets", lambda c, ep, slot: fake_assets(tmp_path, ep))
    calls = []

    def quota(*a, **k):
        calls.append(1)
        raise youtube.QuotaExceededError("配額用盡")

    monkeypatch.setattr(youtube, "upload_video", quota)
    assert run_daily.run_command(ctx, SLOTS) == run_daily.EXIT_FAILED
    assert len(calls) == 1
    assert ctx.state["episodes"]["1"]["attempts"] == 0


def test_auth_checked_before_producing(cfg, notifier, monkeypatch):
    ctx = make_ctx(cfg, notifier)

    def broken_auth():
        raise youtube.AuthError("token 失效")

    ctx.service_factory = broken_auth
    produced = []
    monkeypatch.setattr(run_daily, "produce_episode", lambda c, ep, slot: produced.append(ep))
    assert run_daily.run_command(ctx, SLOTS) == run_daily.EXIT_FAILED
    assert produced == []
    assert "YouTube 認證失敗" in notifier.messages[-1][0]


def test_series_complete_notified_only_once(cfg, notifier):
    ctx = make_ctx(cfg, notifier)
    for ep in range(1, 5):
        state.mark_published(ctx.state, ep)
    assert run_daily.run_command(ctx, SLOTS) == run_daily.EXIT_OK
    assert [m[0] for m in notifier.messages] == ["[影片流水線] 系列完結"]
    assert ctx.state["series_complete_notified"] is True
    run_daily.run_command(ctx, run_daily.compute_slots(date(2026, 9, 27)))
    assert len(notifier.messages) == 1


# ---- verify ----
def test_verify_marks_public_alerts_private_and_survives_errors(cfg, notifier, monkeypatch):
    ctx = make_ctx(cfg, notifier, now=datetime(2026, 9, 27, 14, 30, tzinfo=TAIPEI_TZ))
    state.mark_scheduled(ctx.state, 1, "PUB", "2026-09-26T18:00:00Z", "a")
    state.mark_scheduled(ctx.state, 2, "PRIV", "2026-09-27T06:00:00Z", "b")
    state.mark_uploaded(ctx.state, 3, "BOOM", "2026-09-27T06:00:00Z", "c")
    state.mark_scheduled(ctx.state, 4, "FUTURE", "2026-09-27T18:00:00Z", "d")

    def status(service, vid):
        if vid == "BOOM":
            raise youtube.YouTubeError("500")
        return {"PUB": "public", "PRIV": "private"}[vid]

    monkeypatch.setattr(youtube, "get_privacy_status", status)
    assert run_daily.verify_command(ctx) == run_daily.EXIT_FAILED
    assert ctx.state["episodes"]["1"]["state"] == "published"
    assert ctx.state["episodes"]["2"]["state"] == "scheduled"
    assert ctx.state["episodes"]["4"]["state"] == "scheduled"
    subject, text, _ = notifier.messages[-1]
    assert "警告" in subject and "PRIV" in text and "BOOM" in text and "查詢失敗" in text
    assert state.load_state(cfg.state_path, make_topics(4))["episodes"]["1"]["state"] == "published"


# ---- CLI / 最外層防線 ----
def test_parse_eps():
    assert run_daily.parse_eps("3, 4") == [3, 4]
    assert run_daily.parse_eps(None) == []
    with pytest.raises(Exception):
        run_daily.parse_eps("a,b")


def test_cleanup_work_dir_removes_old_items(tmp_path):
    old_dir = tmp_path / "episodes" / "ep001-old"
    new_dir = tmp_path / "episodes" / "ep002-new"
    old_dir.mkdir(parents=True)
    new_dir.mkdir(parents=True)
    old_time = time.time() - 8 * 86400
    os.utime(old_dir, (old_time, old_time))
    assert run_daily.cleanup_work_dir(tmp_path) == 1
    assert not old_dir.exists() and new_dir.exists()


def _clear_env(monkeypatch):
    for key in ("KINDLE_REPO", "YT_TOKEN_PATH", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                "GMAIL_USER", "GMAIL_APP_PASSWORD", "NOTIFY_EMAIL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(run_daily, "load_dotenv_file", lambda path, env: 0)


def test_main_missing_config_exits_2_and_sends_emergency_notice(monkeypatch, capsys):
    _clear_env(monkeypatch)
    sent = []
    monkeypatch.setattr(notify, "emergency_notify", lambda subject, text, environ=None: sent.append(subject))
    assert run_daily.main(["run"]) == run_daily.EXIT_CONFIG
    assert "設定錯誤" in capsys.readouterr().err
    assert sent and "設定載入失敗" in sent[0]


def test_main_config_failure_in_dry_run_does_not_notify(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr(notify, "emergency_notify", lambda *a, **k: pytest.fail("dry-run 不應通知"))
    assert run_daily.main(["run", "--dry-run"]) == run_daily.EXIT_CONFIG


def _patch_main_config(monkeypatch, cfg, notifier):
    monkeypatch.setattr(run_daily, "_load_cfg_or_notify", lambda args: cfg)
    monkeypatch.setattr(run_daily, "setup_logging", lambda work_dir, today: None)
    monkeypatch.setattr(notify, "Notifier", lambda cfg, dry_run=False: notifier)


def test_main_unexpected_exception_is_logged_and_notified(cfg, notifier, monkeypatch):
    _patch_main_config(monkeypatch, cfg, notifier)

    def explode(args, cfg, notifier):
        raise KeyError("意外")

    monkeypatch.setattr(run_daily, "_dispatch", explode)
    assert run_daily.main(["verify"]) == run_daily.EXIT_FAILED
    subject, text, _ = notifier.messages[-1]
    assert subject.startswith("[影片流水線錯誤]") and "KeyError" in text


def test_lock_busy_over_30_minutes_notifies_once(cfg, notifier, monkeypatch):
    _patch_main_config(monkeypatch, cfg, notifier)
    holder = state.FileLock(cfg.work_dir / "run.lock")
    holder.acquire()
    try:
        marker = cfg.work_dir / "run.lock.busy"
        marker.write_text('{"since": %f, "notified": false}' % (time.time() - 31 * 60), encoding="utf-8")
        assert run_daily.main(["verify"]) == run_daily.EXIT_LOCKED
        assert "30 分鐘" in notifier.messages[-1][0]
        assert run_daily.main(["verify"]) == run_daily.EXIT_LOCKED
        assert len(notifier.messages) == 1
    finally:
        holder.release()


def test_dispatch_rejects_done_eps_without_force(cfg, notifier, monkeypatch, capsys):
    _patch_main_config(monkeypatch, cfg, notifier)
    topics = make_topics(4)
    st = state.init_state(topics)
    state.mark_scheduled(st, 1, "V", SLOTS[0].publish_at, "t")
    state.save_state(cfg.state_path, st)
    monkeypatch.setattr(state, "load_topics", lambda path: topics)
    assert run_daily.main(["run", "--eps", "1"]) == run_daily.EXIT_CONFIG
    assert "--force" in capsys.readouterr().err


def test_failure_after_upload_does_not_demote_episode(cfg, notifier):
    ctx = make_ctx(cfg, notifier)
    state.mark_uploaded(ctx.state, 1, "VID1", SLOTS[0].publish_at, "t")
    run_daily.handle_failure(ctx, 1, SLOTS[0], RuntimeError("上傳後的意外"))
    assert ctx.state["episodes"]["1"]["state"] == "uploaded"
    assert ctx.state["episodes"]["1"]["attempts"] == 0
    assert notifier.messages
