"""state.json 讀寫（原子寫入 + .bak 備份）、集數狀態流轉與 OS 級檔案鎖。

狀態流轉：pending → rendered → uploaded → scheduled → published；失敗記 failed 並累計 attempts。
uploaded 代表 videos.insert 已成功、但字幕 / 縮圖 / 播放清單尚未處理完，同樣視為「該 slot 已有影片」。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import MAX_ATTEMPTS

logger = logging.getLogger(__name__)

ELIGIBLE_STATES = ("pending", "rendered")
DONE_STATES = ("uploaded", "scheduled", "published")
BACKUP_SUFFIX = ".bak"
LOCK_BUSY_ALERT_SECONDS = 30 * 60

if os.name == "posix":
    import fcntl
else:
    import msvcrt


class StateError(Exception):
    """state.json / topics.json 讀寫失敗。"""


class LockBusyError(Exception):
    """已有另一個程序持有鎖。"""


# ---- topics ----
def load_topics(path: Path) -> list[dict[str, Any]]:
    """讀取 topics.json（唯讀）。"""
    try:
        topics = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StateError(f"無法讀取 {path}：{exc}") from exc
    if not isinstance(topics, list) or not topics:
        raise StateError(f"{path} 格式錯誤：應為非空陣列")
    return topics


def _new_entry() -> dict[str, Any]:
    return {"state": "pending", "video_id": None, "publish_at": None,
            "attempts": 0, "last_error": None, "title": None}


def init_state(topics: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """由 topics.json 建立初始 state。"""
    return {"episodes": {str(t["ep"]): _new_entry() for t in topics},
            "playlist_id": None, "series_complete_notified": False}


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + BACKUP_SUFFIX)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StateError(f"{path.name} 損毀或無法讀取：{exc}") from exc


def state_file_missing(path: Path) -> bool:
    """主檔 state.json 不存在 → 上傳前每一集都必須到 YouTube 反查。

    即使有 .bak 也一樣：.bak 比主檔落後一次存檔，可能漏記「已開始上傳」的集數。
    """
    return not path.exists()


def load_state(path: Path, topics: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """讀 state.json；主檔不存在時改用 .bak，兩者都沒有才初始化；並補上 topics 新增的集數。"""
    topics = list(topics)
    if path.exists():
        state = _read_json(path)
    elif backup_path(path).exists():
        logger.warning("state.json 不存在，改用備份 %s", backup_path(path).name)
        state = _read_json(backup_path(path))
    else:
        return init_state(topics)
    episodes = state.setdefault("episodes", {})
    state.setdefault("playlist_id", None)
    state.setdefault("series_complete_notified", False)
    for topic in topics:
        entry_ = episodes.setdefault(str(topic["ep"]), _new_entry())
        for key, value in _new_entry().items():
            entry_.setdefault(key, value)
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    """先把現有檔複製成 .bak，再寫同目錄暫存檔 os.replace，避免寫一半當機留下殘缺 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            shutil.copy2(path, backup_path(path))
        os.replace(tmp_name, path)
    except (OSError, TypeError, ValueError) as exc:
        _silent_unlink(Path(tmp_name))
        raise StateError(f"寫入 state.json 失敗：{exc}") from exc


def _silent_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("無法刪除暫存檔 %s：%s", path, exc)


# ---- 查詢 ----
def entry(state: dict[str, Any], ep: int) -> dict[str, Any]:
    try:
        return state["episodes"][str(ep)]
    except KeyError as exc:
        raise StateError(f"state.json 沒有第 {ep} 集") from exc


def find_done_for_slot(state: dict[str, Any], publish_at: str) -> Optional[int]:
    """若某集已上傳 / 排定 / 公開在此 slot，回傳集數（冪等判斷用）。"""
    for key, item in state["episodes"].items():
        if item.get("publish_at") == publish_at and item.get("state") in DONE_STATES:
            return int(key)
    return None


def is_eligible(item: dict[str, Any]) -> bool:
    if item.get("state") in ELIGIBLE_STATES:
        return True
    return item.get("state") == "failed" and int(item.get("attempts") or 0) < MAX_ATTEMPTS


def is_paused(item: dict[str, Any]) -> bool:
    return item.get("state") == "failed" and int(item.get("attempts") or 0) >= MAX_ATTEMPTS


def next_candidate(state: dict[str, Any], exclude: Iterable[int] = ()) -> Optional[int]:
    """依集數順序取下一個可製作的集數（pending / rendered / failed 未達上限）。"""
    skip = set(exclude)
    for ep in sorted(int(k) for k in state["episodes"]):
        if ep not in skip and is_eligible(state["episodes"][str(ep)]):
            return ep
    return None


def paused_episodes(state: dict[str, Any]) -> list[int]:
    return sorted(int(k) for k, v in state["episodes"].items() if is_paused(v))


def needs_youtube_lookup(item: dict[str, Any]) -> bool:
    """曾渲染、曾上傳或曾失敗的集數，可能已有影片在 YouTube 上（state 沒記到），上傳前要反查。"""
    if item.get("state") in ("rendered", "uploaded"):
        return True
    return int(item.get("attempts") or 0) > 0 or bool(item.get("last_error"))


def previous_titles(state: dict[str, Any], exclude_ep: Optional[int] = None) -> list[str]:
    """既往集數已用過的標題（QA 檢查重複用）。"""
    titles = []
    for key, item in state["episodes"].items():
        if exclude_ep is not None and int(key) == exclude_ep:
            continue
        if item.get("title"):
            titles.append(item["title"])
    return titles


# ---- 狀態流轉 ----
def mark_rendered(state: dict[str, Any], ep: int, title: str) -> None:
    entry(state, ep).update(state="rendered", title=title)


def mark_uploaded(state: dict[str, Any], ep: int, video_id: str, publish_at: str, title: str) -> None:
    entry(state, ep).update(state="uploaded", video_id=video_id, publish_at=publish_at, title=title)


def mark_scheduled(state: dict[str, Any], ep: int, video_id: str, publish_at: str, title: str) -> None:
    item = entry(state, ep)
    item.update(state="scheduled", video_id=video_id, publish_at=publish_at, title=title, last_error=None)


def mark_published(state: dict[str, Any], ep: int, video_id: Optional[str] = None) -> None:
    item = entry(state, ep)
    item["state"] = "published"
    if video_id:
        item["video_id"] = video_id


def mark_failed(state: dict[str, Any], ep: int, error: str, count_attempt: bool = True) -> int:
    """記錄失敗；count_attempt=False 用於配額 / 授權等非本集內容造成的失敗。回傳累計次數。"""
    item = entry(state, ep)
    if count_attempt:
        item["attempts"] = int(item.get("attempts") or 0) + 1
        item["state"] = "failed"
    else:
        item["state"] = "pending" if int(item.get("attempts") or 0) == 0 else "failed"
    item["last_error"] = error[-2000:]
    return int(item["attempts"])


# ---- OS 級檔案鎖 ----
def _os_lock(fd: int) -> None:
    if os.name == "posix":
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)


def _os_unlock(fd: int) -> None:
    if os.name == "posix":
        fcntl.flock(fd, fcntl.LOCK_UN)
    else:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


class FileLock:
    """鎖在開啟中的 fd 上（Linux flock / Windows msvcrt.locking）：行程死亡時由 OS 自動釋放，不會殘留假鎖。"""

    def __init__(self, path: Path):
        self.path = path
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _os_lock(fd)
        except OSError as exc:
            os.close(fd)
            raise LockBusyError(f"另一個程序正在執行（鎖檔 {self.path}）") from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()} {time.time():.0f}\n".encode("ascii"))
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            _os_unlock(self._fd)
        except OSError as exc:
            logger.warning("解鎖失敗（關閉 fd 後 OS 仍會釋放）：%s", exc)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


# ---- 鎖忙碌追蹤（持續卡住要通知）----
def record_lock_busy(marker: Path, now: Optional[float] = None) -> tuple[float, bool]:
    """記錄第一次遇到鎖忙碌的時間；回傳（已忙碌秒數, 是否該發通知）。每段忙碌期間只通知一次。"""
    now = time.time() if now is None else now
    data = {"since": now, "notified": False}
    try:
        data.update(json.loads(marker.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        logger.warning("鎖忙碌紀錄損毀，重新計時：%s", exc)
    busy = now - float(data["since"])
    should_alert = busy >= LOCK_BUSY_ALERT_SECONDS and not data["notified"]
    if should_alert:
        data["notified"] = True
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(data), encoding="utf-8")
    except OSError as exc:
        logger.warning("無法寫入鎖忙碌紀錄：%s", exc)
    return busy, should_alert


def clear_lock_busy(marker: Path) -> None:
    _silent_unlink(marker)
