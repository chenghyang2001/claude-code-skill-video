"""YouTube：上傳（預約公開）、重複上傳反查、字幕、縮圖、播放清單、公開後驗證（SPEC §10）。"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from .config import SERIES_NAME

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
CHUNK_SIZE = 4 * 1024 * 1024
MAX_RETRIES = 3
BACKOFF_BASE = 2.0
TITLE_MAX = 100
DESCRIPTION_MAX = 4700
TAGS_MAX_CHARS = 450
LOOKUP_MAX_PAGES = 4                     # 反查最近 200 支上傳影片就夠（每天只上傳 2 支）
QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded"})
AUTH_REASONS = frozenset({"authError", "unauthorized", "forbiddenByOwner"})
SERIES_TAGS = ("Claude Code", "Claude Code Skill", "AI 教學", "Claude")
DESCRIPTION_FOOTER = (
    "\n\n────────────\n"
    f"【系列介紹】「{SERIES_NAME}」每集挑 1～2 個 Claude Code Skill 實際跑一次，"
    "整理用法、實測結果與踩到的坑。\n"
    "【聲明】本影片由 AI 自動生成：腳本、投影片、語音與字幕皆為自動產生，內容以實測紀錄為準。\n"
    "【實測來源】kindle-98 repo：逐一實測《AI Skill 10 倍速指南》書附 Skill 的 Demo 紀錄。"
)


class YouTubeError(Exception):
    """YouTube API 失敗。"""


class QuotaExceededError(YouTubeError):
    """配額 / 上傳次數用盡：不重試。"""


class AuthError(YouTubeError):
    """token 無效、refresh 失敗或 401：需要重新授權。"""


@dataclass
class ExistingVideo:
    """反查到的既有影片。"""

    video_id: str
    privacy_status: Optional[str]
    publish_at: Optional[str]


# ---- 內容組裝 ----
def _sanitize(text: str) -> str:
    # YouTube 標題 / 描述不接受半形角括號
    return text.replace("<", "＜").replace(">", "＞")


def marker_tag(ep: int) -> str:
    return f"skillvideo-ep{ep:03d}"


def marker_text(ep: int) -> str:
    """寫在描述尾端的唯一標記，供 state.json 遺失時到 YouTube 反查。"""
    return f"[skillvideo:ep{ep:03d}]"


def build_full_title(ep: int, title: str) -> str:
    """`Claude Code Skill 技術探討 #001｜<skill 名>：<一句重點>`，超過 100 字元截斷。"""
    full = _sanitize(f"{SERIES_NAME} #{ep:03d}｜{title.strip()}")
    return full if len(full) <= TITLE_MAX else full[:TITLE_MAX - 1] + "…"


def build_description(description: str, ep: int) -> str:
    return _sanitize(description.strip())[:DESCRIPTION_MAX] + DESCRIPTION_FOOTER + "\n" + marker_text(ep)


def build_tags(tags: Sequence[str], ep: int) -> list[str]:
    """唯一標記 tag 永遠排第一，再合併系列標籤並去重，總長限制在 YouTube 上限內。"""
    result, used = [], 0
    for tag in [marker_tag(ep), *SERIES_TAGS, *tags]:
        tag = _sanitize(str(tag)).strip()
        if not tag or tag in result:
            continue
        cost = len(tag) + 3  # 含空白的標籤 YouTube 會加引號與逗號
        if used + cost > TAGS_MAX_CHARS:
            break
        result.append(tag)
        used += cost
    return result


def build_video_body(full_title: str, description: str, tags: Sequence[str], publish_at: str,
                     ep: int) -> dict[str, Any]:
    """videos.insert 的 body：私人 + publishAt 預約公開、標示合成內容、帶唯一標記。"""
    return {
        "snippet": {"title": full_title, "description": build_description(description, ep),
                    "tags": build_tags(tags, ep), "categoryId": "27",
                    "defaultLanguage": "zh-Hant", "defaultAudioLanguage": "zh-Hant"},
        "status": {"privacyStatus": "private", "publishAt": publish_at,
                   "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True},
    }


# ---- 認證 ----
def _save_token(path: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".token-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("refresh 後的 token 無法寫回（下次會再 refresh）：%s", exc)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def load_credentials(token_path: Path) -> Credentials:
    """讀 OAuth token；過期就 refresh，refresh 失敗拋 AuthError。"""
    try:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except (OSError, ValueError) as exc:
        raise AuthError(f"無法讀取 YouTube token：{exc}") from exc
    if creds.valid:
        return creds
    if not creds.refresh_token:
        raise AuthError("YouTube token 沒有 refresh_token，需要重新授權")
    try:
        creds.refresh(Request())
    except RefreshError as exc:
        raise AuthError(f"YouTube token refresh 失敗，需要重新授權：{exc}") from exc
    except TransportError as exc:
        raise YouTubeError(f"refresh token 時網路錯誤：{exc}") from exc
    _save_token(token_path, creds.to_json())
    return creds


def build_service(creds: Credentials) -> Any:
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


# ---- 錯誤分類與重試 ----
def classify_http_error(err: HttpError) -> tuple[int, str]:
    """回傳（HTTP 狀態碼, 第一個 error reason）。"""
    status = int(getattr(err.resp, "status", 0) or 0)
    reason = ""
    try:
        payload = json.loads(err.content.decode("utf-8"))
        details = payload.get("error", {}).get("errors") or []
        reason = details[0].get("reason", "") if details else ""
    except (ValueError, AttributeError, UnicodeDecodeError, TypeError, IndexError):
        pass
    return status, reason


def raise_for_http_error(err: HttpError, can_retry: bool) -> None:
    """依 reason / 狀態碼轉成對應例外；可重試的 5xx 直接 return 讓呼叫端重試。"""
    status, reason = classify_http_error(err)
    # uploadLimitExceeded 實際回 400，所以配額一律看 reason 不看狀態碼
    if reason in QUOTA_REASONS:
        raise QuotaExceededError(f"YouTube 配額 / 上傳次數用盡（{status} {reason}）") from err
    if status == 401 or reason in AUTH_REASONS:
        raise AuthError(f"YouTube 認證失敗，需要重新授權（{status} {reason}）") from err
    if not 500 <= status < 600 or not can_retry:
        raise YouTubeError(f"YouTube API 錯誤 {status} {reason}".strip()) from err


def execute_with_retry(call: Callable[[], Any], sleep: Callable[[float], None] = time.sleep,
                       retries: int = MAX_RETRIES) -> Any:
    """配額 / 認證錯誤立即拋出不重試；5xx 與網路錯誤指數退避重試 retries 次。"""
    for attempt in range(retries + 1):
        try:
            return call()
        except HttpError as err:
            raise_for_http_error(err, can_retry=attempt < retries)
            logger.warning("YouTube 5xx，%.0f 秒後重試（%d/%d）", BACKOFF_BASE ** attempt, attempt + 1, retries)
        except (OSError, TimeoutError) as err:
            if attempt >= retries:
                raise YouTubeError(f"YouTube 連線失敗：{err}") from err
            logger.warning("YouTube 連線錯誤，重試（%d/%d）：%s", attempt + 1, retries, err)
        sleep(BACKOFF_BASE ** attempt)
    raise YouTubeError("重試次數用盡")


# ---- 反查既有影片（防重複上傳）----
def _uploads_playlist_id(service: Any) -> Optional[str]:
    response = execute_with_retry(service.channels().list(part="contentDetails", mine=True).execute)
    items = response.get("items") or []
    if not items:
        return None
    return items[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")


def _scan_uploads(service: Any, uploads_id: str, marker: str) -> Optional[str]:
    token: Optional[str] = None
    for _ in range(LOOKUP_MAX_PAGES):
        kwargs = {"part": "snippet", "playlistId": uploads_id, "maxResults": 50}
        if token:
            kwargs["pageToken"] = token
        response = execute_with_retry(service.playlistItems().list(**kwargs).execute)
        for item in response.get("items") or []:
            snippet = item.get("snippet", {})
            if marker in (snippet.get("description") or ""):
                return snippet.get("resourceId", {}).get("videoId")
        token = response.get("nextPageToken")
        if not token:
            return None
    return None


def find_uploaded_video(service: Any, ep: int) -> Optional[ExistingVideo]:
    """在自己頻道的 uploads 清單（含私人影片）找描述帶 [skillvideo:epNNN] 的影片。"""
    uploads_id = _uploads_playlist_id(service)
    if not uploads_id:
        return None
    video_id = _scan_uploads(service, uploads_id, marker_text(ep))
    if not video_id:
        return None
    status = get_video_status(service, video_id)
    return ExistingVideo(video_id, status.get("privacyStatus"), status.get("publishAt"))


def reschedule_video(service: Any, video_id: str, publish_at: str) -> None:
    """既有私人影片改排到新的 publishAt（status 需整組送出）。"""
    body = {"id": video_id, "status": {"privacyStatus": "private", "publishAt": publish_at,
                                       "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True}}
    execute_with_retry(service.videos().update(part="status", body=body).execute)


# ---- 上傳與上傳後步驟 ----
def upload_video(service: Any, video_path: Path, body: dict[str, Any],
                 sleep: Callable[[float], None] = time.sleep) -> str:
    """resumable 上傳（4MB chunk），回傳 video_id。"""
    media = MediaFileUpload(str(video_path), mimetype="video/mp4", chunksize=CHUNK_SIZE, resumable=True)
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        progress, response = execute_with_retry(request.next_chunk, sleep=sleep)
        if progress is not None:
            logger.info("上傳進度 %d%%", int(progress.progress() * 100))
    video_id = response.get("id") if isinstance(response, dict) else None
    if not video_id:
        raise YouTubeError(f"上傳回應沒有影片 ID：{response}")
    return video_id


def upload_caption(service: Any, video_id: str, srt_path: Path) -> None:
    body = {"snippet": {"videoId": video_id, "language": "zh-Hant", "name": "繁體中文", "isDraft": False}}
    media = MediaFileUpload(str(srt_path), mimetype="application/octet-stream", resumable=False)
    execute_with_retry(service.captions().insert(part="snippet", body=body, media_body=media).execute)


def set_thumbnail(service: Any, video_id: str, thumb_path: Path) -> None:
    media = MediaFileUpload(str(thumb_path), mimetype="image/jpeg", resumable=False)
    execute_with_retry(service.thumbnails().set(videoId=video_id, media_body=media).execute)


def ensure_playlist(service: Any, playlist_id: Optional[str]) -> str:
    """沒有播放清單就建立公開的系列清單。"""
    if playlist_id:
        return playlist_id
    body = {"snippet": {"title": SERIES_NAME, "defaultLanguage": "zh-Hant",
                        "description": f"{SERIES_NAME}：每天兩集，實測 Claude Code Skill。"},
            "status": {"privacyStatus": "public"}}
    response = execute_with_retry(service.playlists().insert(part="snippet,status", body=body).execute)
    return response["id"]


def add_to_playlist(service: Any, playlist_id: str, video_id: str) -> None:
    body = {"snippet": {"playlistId": playlist_id,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id}}}
    execute_with_retry(service.playlistItems().insert(part="snippet", body=body).execute)


def _soft_step(label: str, func: Callable[[], Any]) -> Any:
    """上傳後的步驟：影片已存在，任何錯誤都只能降級成警告，絕不能讓流程重新上傳。"""
    try:
        return func()
    except Exception as exc:  # noqa: BLE001 — 刻意全接：此時拋錯會導致重複上傳
        logger.warning("%s失敗（影片已上傳，不擋上架）：%s: %s", label, type(exc).__name__, exc)
        return None


def is_in_playlist(service: Any, playlist_id: str, video_id: str) -> bool:
    """playlistItems.list 以 videoId 過濾，確認影片是否已在清單內。"""
    response = execute_with_retry(service.playlistItems().list(
        part="id", playlistId=playlist_id, videoId=video_id, maxResults=1).execute)
    return bool(response.get("items"))


def _add_to_playlist_once(service: Any, playlist_id: str, video_id: str, check_playlist: bool) -> None:
    if check_playlist and is_in_playlist(service, playlist_id, video_id):
        logger.info("影片 %s 已在播放清單內，略過加入", video_id)
        return
    add_to_playlist(service, playlist_id, video_id)


def finish_upload(service: Any, video_id: str, srt_path: Path, thumb_path: Path,
                  playlist_id: Optional[str], check_playlist: bool = False) -> Optional[str]:
    """字幕 → 縮圖 → 播放清單，全部降級為警告；回傳（可能新建的）playlist_id。

    check_playlist=True（沿用既有影片時）會先查是否已在清單內，避免同一支影片加入兩次。
    """
    _soft_step("字幕上傳", lambda: upload_caption(service, video_id, srt_path))
    _soft_step("縮圖設定", lambda: set_thumbnail(service, video_id, thumb_path))
    new_id = _soft_step("建立播放清單", lambda: ensure_playlist(service, playlist_id))
    if new_id:
        _soft_step("加入播放清單", lambda: _add_to_playlist_once(service, new_id, video_id, check_playlist))
    return new_id or playlist_id


def get_video_status(service: Any, video_id: str) -> dict[str, Any]:
    """回傳影片 status 物件；影片不存在回傳空 dict。"""
    response = execute_with_retry(service.videos().list(part="status", id=video_id).execute)
    items = response.get("items") or []
    return items[0].get("status", {}) if items else {}


def get_privacy_status(service: Any, video_id: str) -> Optional[str]:
    """回傳 privacyStatus；影片不存在回傳 None。"""
    return get_video_status(service, video_id).get("privacyStatus")


def video_url(video_id: str) -> str:
    return f"https://youtu.be/{video_id}"
