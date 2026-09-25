"""state：初始化、原子寫入、候選集數與重試上限、冪等判斷、檔案鎖。"""
from __future__ import annotations

import json
import os
import time

import pytest

from conftest import make_topics
from skillvideo import state


def test_load_state_initializes_and_merges_new_topics(tmp_path):
    path = tmp_path / "state.json"
    st = state.load_state(path, make_topics(2))
    assert st["episodes"]["1"]["state"] == "pending"
    state.save_state(path, st)
    merged = state.load_state(path, make_topics(3))
    assert set(merged["episodes"]) == {"1", "2", "3"}
    assert merged["playlist_id"] is None


def test_save_state_is_atomic_and_leaves_no_temp(tmp_path):
    path = tmp_path / "state.json"
    st = state.init_state(make_topics(2))
    state.save_state(path, st)
    assert json.loads(path.read_text(encoding="utf-8")) == st
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_save_state_failure_keeps_original(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    original = state.init_state(make_topics(1))
    state.save_state(path, original)

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(state.os, "replace", broken_replace)
    changed = state.init_state(make_topics(3))
    with pytest.raises(state.StateError):
        state.save_state(path, changed)
    assert json.loads(path.read_text(encoding="utf-8")) == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json", "state.json.bak"]


def test_corrupt_state_raises(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(state.StateError):
        state.load_state(path, make_topics(1))


def test_next_candidate_respects_failed_retry_limit():
    st = state.init_state(make_topics(4))
    st["episodes"]["1"].update(state="failed", attempts=3)   # 已達上限：暫停
    st["episodes"]["2"].update(state="failed", attempts=2)   # 還能重試
    st["episodes"]["3"].update(state="scheduled")
    assert state.next_candidate(st) == 2
    assert state.next_candidate(st, exclude=[2]) == 4
    assert state.next_candidate(st, exclude=[2, 4]) is None


def test_mark_failed_counts_attempts_and_can_skip_counting():
    st = state.init_state(make_topics(1))
    assert state.mark_failed(st, 1, "boom") == 1
    assert state.mark_failed(st, 1, "boom") == 2
    assert state.mark_failed(st, 1, "quota", count_attempt=False) == 2
    assert st["episodes"]["1"]["state"] == "failed"
    fresh = state.init_state(make_topics(1))
    state.mark_failed(fresh, 1, "quota", count_attempt=False)
    assert fresh["episodes"]["1"]["state"] == "pending"


def test_find_done_for_slot_is_idempotency_key():
    st = state.init_state(make_topics(2))
    state.mark_scheduled(st, 2, "vid", "2026-09-26T18:00:00Z", "標題")
    assert state.find_done_for_slot(st, "2026-09-26T18:00:00Z") == 2
    assert state.find_done_for_slot(st, "2026-09-27T06:00:00Z") is None
    st["episodes"]["2"]["state"] = "failed"
    assert state.find_done_for_slot(st, "2026-09-26T18:00:00Z") is None


def test_previous_titles_excludes_current_episode():
    st = state.init_state(make_topics(3))
    state.mark_rendered(st, 1, "A")
    state.mark_rendered(st, 2, "B")
    assert sorted(state.previous_titles(st, exclude_ep=2)) == ["A"]


def test_file_lock_blocks_second_holder_and_releases(tmp_path):
    lock_path = tmp_path / "run.lock"
    with state.FileLock(lock_path):
        with pytest.raises(state.LockBusyError):
            state.FileLock(lock_path).acquire()
    # 鎖檔本身保留（刪除被 flock 的檔案會有競態），但釋放後可以再次取得
    with state.FileLock(lock_path):
        assert lock_path.exists()


def test_file_lock_released_when_holder_dies_without_unlock(tmp_path):
    """模擬行程死亡：不呼叫 release、直接關 fd，OS 必須自動釋放鎖（舊版 O_EXCL 鎖檔會殘留）。"""
    lock_path = tmp_path / "run.lock"
    holder = state.FileLock(lock_path)
    holder.acquire()
    with pytest.raises(state.LockBusyError):
        state.FileLock(lock_path).acquire()
    os.close(holder._fd)
    holder._fd = None
    # Windows 文件：關閉 handle 後由 OS 解鎖，但時間點視系統資源而定，給一點緩衝
    relock = state.FileLock(lock_path)
    for _ in range(50):
        try:
            relock.acquire()
            break
        except state.LockBusyError:
            time.sleep(0.1)
    assert relock._fd is not None
    relock.release()


def test_save_state_keeps_backup_and_load_falls_back(tmp_path):
    path = tmp_path / "state.json"
    first = state.init_state(make_topics(2))
    state.save_state(path, first)
    second = state.init_state(make_topics(2))
    state.mark_scheduled(second, 1, "VID", "2026-09-26T18:00:00Z", "t")
    state.save_state(path, second)
    backup = state.backup_path(path)
    assert json.loads(backup.read_text(encoding="utf-8")) == first
    path.unlink()
    # 只剩 .bak 仍算主檔遺失：.bak 落後一次存檔，可能漏記已開始上傳的集數
    assert state.state_file_missing(path)
    assert state.load_state(path, make_topics(2))["episodes"]["1"]["state"] == "pending"
    backup.unlink()
    assert state.state_file_missing(path)


def test_uploaded_counts_as_done_and_triggers_lookup():
    st = state.init_state(make_topics(2))
    state.mark_uploaded(st, 1, "VID", "2026-09-26T18:00:00Z", "t")
    assert state.find_done_for_slot(st, "2026-09-26T18:00:00Z") == 1
    assert state.needs_youtube_lookup(st["episodes"]["1"])
    assert not state.needs_youtube_lookup(st["episodes"]["2"])
    st["episodes"]["2"]["attempts"] = 1
    assert state.needs_youtube_lookup(st["episodes"]["2"])


def test_record_lock_busy_alerts_once_after_30_minutes(tmp_path):
    marker = tmp_path / "run.lock.busy"
    assert state.record_lock_busy(marker, now=1000.0) == (0.0, False)
    busy, alert = state.record_lock_busy(marker, now=1000.0 + 31 * 60)
    assert alert and busy >= 30 * 60
    assert state.record_lock_busy(marker, now=1000.0 + 60 * 60)[1] is False
    state.clear_lock_busy(marker)
    assert not marker.exists()
