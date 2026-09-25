"""youtube：上傳 body 與唯一標記、標題格式、錯誤分類（配額 / 認證 / 5xx）、resumable 上傳、
上傳後步驟降級、既有影片反查、token refresh。"""
from __future__ import annotations

import json

import httplib2
import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from skillvideo import youtube


def http_error(status: int, reason: str = "") -> HttpError:
    content = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode("utf-8")
    return HttpError(httplib2.Response({"status": status}), content)


def test_video_body_has_publish_at_synthetic_flag_and_marker():
    body = youtube.build_video_body("標題", "描述", ["paperjsx"], "2026-09-26T18:00:00Z", 58)
    status, snippet = body["status"], body["snippet"]
    assert status["privacyStatus"] == "private"
    assert status["publishAt"] == "2026-09-26T18:00:00Z"
    assert status["containsSyntheticMedia"] is True
    assert status["selfDeclaredMadeForKids"] is False
    assert snippet["categoryId"] == "27" and snippet["defaultLanguage"] == "zh-Hant"
    assert "AI 自動生成" in snippet["description"]
    assert snippet["description"].endswith("[skillvideo:ep058]")
    assert snippet["tags"][0] == "skillvideo-ep058"
    assert "paperjsx" in snippet["tags"] and "Claude Code" in snippet["tags"]


def test_marker_tag_survives_many_tags():
    tags = youtube.build_tags([f"很長的標籤{i}" * 3 for i in range(80)], 7)
    assert tags[0] == "skillvideo-ep007"
    assert sum(len(t) + 3 for t in tags) <= youtube.TAGS_MAX_CHARS


def test_full_title_format_and_limit():
    assert youtube.build_full_title(1, "paperjsx：一句話做簡報") == "Claude Code Skill 技術探討 #001｜paperjsx：一句話做簡報"
    long_title = youtube.build_full_title(73, "很" * 200)
    assert len(long_title) <= 100 and long_title.startswith("Claude Code Skill 技術探討 #073｜")
    assert "<" not in youtube.build_full_title(2, "a<b>")


def test_quota_error_is_not_retried():
    calls, sleeps = [], []

    def call():
        calls.append(1)
        raise http_error(403, "quotaExceeded")

    with pytest.raises(youtube.QuotaExceededError):
        youtube.execute_with_retry(call, sleep=sleeps.append)
    assert len(calls) == 1 and sleeps == []


def test_upload_limit_400_is_quota_by_reason():
    """重現：uploadLimitExceeded 實際回 400，舊版只看 403 會誤判成一般錯誤。"""
    with pytest.raises(youtube.QuotaExceededError):
        youtube.execute_with_retry(lambda: (_ for _ in ()).throw(http_error(400, "uploadLimitExceeded")),
                                   sleep=lambda s: None)


def test_401_and_auth_error_reason_are_auth_errors():
    for err in (http_error(401, "authError"), http_error(403, "authError"), http_error(401)):
        calls = []

        def call(err=err):
            calls.append(1)
            raise err

        with pytest.raises(youtube.AuthError):
            youtube.execute_with_retry(call, sleep=lambda s: None)
        assert len(calls) == 1


def test_5xx_is_retried_with_backoff_then_succeeds():
    calls, sleeps = [], []

    def call():
        calls.append(1)
        if len(calls) < 3:
            raise http_error(503)
        return {"id": "ok"}

    assert youtube.execute_with_retry(call, sleep=sleeps.append) == {"id": "ok"}
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]


def test_5xx_exhausted_and_4xx_not_retried():
    calls = []

    def always_500():
        calls.append(1)
        raise http_error(500)

    with pytest.raises(youtube.YouTubeError):
        youtube.execute_with_retry(always_500, sleep=lambda s: None)
    assert len(calls) == 4       # 首次 + 重試 3 次

    calls.clear()

    def not_found():
        calls.append(1)
        raise http_error(404, "notFound")

    with pytest.raises(youtube.YouTubeError, match="404"):
        youtube.execute_with_retry(not_found, sleep=lambda s: None)
    assert len(calls) == 1


class FakeRequest:
    def __init__(self, responses):
        self.responses = list(responses)

    def next_chunk(self):
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def execute(self):
        return self.next_chunk()


class FakeProgress:
    def progress(self):
        return 0.5


class Chunks(list):
    """標記：這串回應屬於同一個 resumable 上傳 request。"""


class FakeResource:
    """以 method 名稱查表回應的假資源（videos / captions / thumbnails / playlists / ...）。"""

    def __init__(self, service, name):
        self.service, self.name = service, name

    def __getattr__(self, method):
        def call(**kwargs):
            key = f"{self.name}.{method}"
            self.service.calls.append((key, kwargs))
            responses = self.service.responses.get(key, [{}])
            if isinstance(responses, Exception):
                raise responses
            if isinstance(responses, Chunks):
                return FakeRequest(responses)          # 同一個 request 多次 next_chunk
            # 一般呼叫：每次呼叫取下一個回應，最後一個重複使用
            return FakeRequest([responses.pop(0) if len(responses) > 1 else responses[0]])
        return call


class FakeService:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __getattr__(self, name):
        return lambda: FakeResource(self, name)


@pytest.fixture
def media_files(tmp_path):
    paths = {}
    for name in ("v.mp4", "v.srt", "t.jpg"):
        path = tmp_path / name
        path.write_bytes(b"data")
        paths[name] = path
    return paths


def test_upload_video_loops_next_chunk_and_retries_5xx(media_files):
    service = FakeService({"videos.insert": Chunks([(FakeProgress(), None), http_error(502), (None, {"id": "VID"})])})
    body = youtube.build_video_body("t", "d", [], "2026-09-26T18:00:00Z", 1)
    assert youtube.upload_video(service, media_files["v.mp4"], body, sleep=lambda s: None) == "VID"
    name, kwargs = service.calls[0]
    assert kwargs["part"] == "snippet,status" and kwargs["body"] is body


def test_finish_upload_downgrades_every_failure(media_files):
    """重現：上傳後步驟丟非預期例外（例如 KeyError / RuntimeError）會讓整集被判失敗而重傳。"""
    service = FakeService({"captions.insert": [http_error(400, "invalid")],
                           "thumbnails.set": RuntimeError("非預期"),
                           "playlists.insert": [{"no_id": True}]})
    playlist = youtube.finish_upload(service, "VID", media_files["v.srt"], media_files["t.jpg"], None)
    assert playlist is None
    names = [c[0] for c in service.calls]
    assert names == ["captions.insert", "thumbnails.set", "playlists.insert"]


def test_finish_upload_creates_public_playlist(media_files):
    service = FakeService({"playlists.insert": [{"id": "PL1"}]})
    assert youtube.finish_upload(service, "VID", media_files["v.srt"], media_files["t.jpg"], None) == "PL1"
    names = [c[0] for c in service.calls]
    assert names[-2:] == ["playlists.insert", "playlistItems.insert"]
    assert service.calls[-2][1]["body"]["status"]["privacyStatus"] == "public"


def test_finish_upload_skips_playlist_insert_when_already_listed(media_files):
    service = FakeService({"playlistItems.list": [{"items": [{"id": "PLI1"}]}]})
    youtube.finish_upload(service, "VID", media_files["v.srt"], media_files["t.jpg"], "PL1", check_playlist=True)
    names = [c[0] for c in service.calls]
    assert "playlistItems.list" in names and "playlistItems.insert" not in names
    list_kwargs = dict(service.calls)["playlistItems.list"]
    assert list_kwargs["playlistId"] == "PL1" and list_kwargs["videoId"] == "VID"


def test_finish_upload_adds_when_reused_video_not_listed(media_files):
    service = FakeService({"playlistItems.list": [{"items": []}]})
    youtube.finish_upload(service, "VID", media_files["v.srt"], media_files["t.jpg"], "PL1", check_playlist=True)
    names = [c[0] for c in service.calls]
    assert names[-2:] == ["playlistItems.list", "playlistItems.insert"]


def test_find_uploaded_video_by_description_marker():
    service = FakeService({
        "channels.list": [{"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]}],
        "playlistItems.list": [
            {"items": [{"snippet": {"description": "別集\n[skillvideo:ep003]", "resourceId": {"videoId": "V3"}}}],
             "nextPageToken": "P2"},
        ],
        "videos.list": [{"items": [{"status": {"privacyStatus": "private", "publishAt": "2026-09-26T18:00:00Z"}}]}],
    })
    # 第二頁才有目標影片
    service.responses["playlistItems.list"].append(
        {"items": [{"snippet": {"description": "本集\n[skillvideo:ep058]", "resourceId": {"videoId": "V58"}}}]})
    found = youtube.find_uploaded_video(service, 58)
    assert found == youtube.ExistingVideo("V58", "private", "2026-09-26T18:00:00Z")
    list_calls = [c for c in service.calls if c[0] == "playlistItems.list"]
    assert list_calls[0][1]["playlistId"] == "UU1" and list_calls[1][1]["pageToken"] == "P2"


def test_find_uploaded_video_none_when_absent():
    service = FakeService({
        "channels.list": [{"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]}],
        "playlistItems.list": [{"items": []}],
    })
    assert youtube.find_uploaded_video(service, 58) is None


class FakeCreds:
    def __init__(self, valid, refresh_token="r", fail=False):
        self.valid = valid
        self.refresh_token = refresh_token
        self.fail = fail
        self.refreshed = False

    def refresh(self, request):
        if self.fail:
            raise RefreshError("invalid_grant")
        self.refreshed = True
        self.valid = True

    def to_json(self):
        return '{"token": "new"}'


def test_load_credentials_refreshes_and_saves(tmp_path, monkeypatch):
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")
    creds = FakeCreds(valid=False)
    monkeypatch.setattr(youtube.Credentials, "from_authorized_user_file", lambda path, scopes: creds)
    assert youtube.load_credentials(token) is creds
    assert creds.refreshed
    assert json.loads(token.read_text(encoding="utf-8")) == {"token": "new"}


def test_save_token_failure_removes_temp_file(tmp_path, monkeypatch):
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(youtube.os, "replace", broken_replace)
    youtube._save_token(token, '{"token": "new"}')
    assert sorted(p.name for p in tmp_path.iterdir()) == ["token.json"]


def test_load_credentials_refresh_failure_needs_reauth(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube.Credentials, "from_authorized_user_file",
                        lambda path, scopes: FakeCreds(valid=False, fail=True))
    with pytest.raises(youtube.AuthError, match="重新授權"):
        youtube.load_credentials(tmp_path / "token.json")
    monkeypatch.setattr(youtube.Credentials, "from_authorized_user_file",
                        lambda path, scopes: FakeCreds(valid=False, refresh_token=None))
    with pytest.raises(youtube.AuthError):
        youtube.load_credentials(tmp_path / "token.json")
