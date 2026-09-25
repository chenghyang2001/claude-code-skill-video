"""通知：Telegram Bot API（純文字）+ Gmail SMTP SSL（HTML + 圖片附件）。

通知失敗只記 log，不中斷主流程；網路錯誤會在程式內重試（systemd user unit 無法可靠等待網路就緒）。
"""
from __future__ import annotations

import html
import json
import logging
import os
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from .config import TAIPEI_TZ, tail_text

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_MAX = 4000
ATTACHMENT_MAX_BYTES = 300 * 1024
NETWORK_TIMEOUT = 30
NETWORK_RETRIES = 3
RETRY_INTERVAL = 5.0
PERMANENT_SMTP_ERRORS = (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused,
                         smtplib.SMTPSenderRefused, smtplib.SMTPDataError)


@dataclass
class Channels:
    """通知管道憑證（可由 Config 或直接由環境變數組成）。"""

    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    gmail_user: Optional[str] = None
    gmail_app_password: Optional[str] = None
    notify_email: Optional[str] = None


def channels_from_environ(environ: Optional[Mapping[str, str]] = None) -> Channels:
    """設定載入失敗時用：直接從環境變數取通知憑證，能湊出哪個管道就用哪個。"""
    env = os.environ if environ is None else environ

    def get(key: str) -> Optional[str]:
        value = (env.get(key) or "").strip()
        return value or None

    return Channels(get("TELEGRAM_BOT_TOKEN"), get("TELEGRAM_CHAT_ID"), get("GMAIL_USER"),
                    get("GMAIL_APP_PASSWORD"), get("NOTIFY_EMAIL"))


def _with_retry(label: str, action: Callable[[], bool], retryable: tuple[type[BaseException], ...],
                sleep: Callable[[float], None]) -> bool:
    for attempt in range(1, NETWORK_RETRIES + 1):
        try:
            return action()
        except retryable as exc:
            # 例外訊息可能含網址（內含 bot token），只記類型
            logger.warning("%s 失敗（第 %d/%d 次）：%s", label, attempt, NETWORK_RETRIES, type(exc).__name__)
            if attempt < NETWORK_RETRIES:
                sleep(RETRY_INTERVAL)
    logger.error("%s 重試 %d 次仍失敗", label, NETWORK_RETRIES)
    return False


def send_telegram(token: str, chat_id: str, text: str,
                  opener: Callable[..., Any] = urllib.request.urlopen,
                  sleep: Callable[[float], None] = time.sleep) -> bool:
    """送 Telegram 純文字訊息；成功回傳 True，任何失敗記 log 回傳 False。"""
    payload = json.dumps({"chat_id": chat_id, "text": text[:TELEGRAM_MAX],
                          "disable_web_page_preview": True}).encode("utf-8")
    request = urllib.request.Request(TELEGRAM_API.format(token=token), data=payload,
                                     headers={"Content-Type": "application/json"})

    def attempt() -> bool:
        with opener(request, timeout=NETWORK_TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8"))
        if not body.get("ok"):
            logger.error("Telegram 回應失敗：%s", body.get("description"))
        return bool(body.get("ok"))

    return _with_retry("Telegram 通知", attempt, (urllib.error.URLError, OSError, ValueError), sleep)


def _attach_images(msg: EmailMessage, attachments: Sequence[Path]) -> None:
    for path in attachments:
        try:
            size = path.stat().st_size
            if size > ATTACHMENT_MAX_BYTES:
                logger.warning("附件 %s 超過 300KB（%d bytes），略過", path.name, size)
                continue
            data = path.read_bytes()
        except OSError as exc:
            logger.warning("附件 %s 無法讀取：%s", path.name, exc)
            continue
        subtype = "png" if path.suffix.lower() == ".png" else "jpeg"
        msg.add_attachment(data, maintype="image", subtype=subtype, filename=path.name)


def build_email(sender: str, to: str, subject: str, text: str, attachments: Sequence[Path]) -> EmailMessage:
    """純文字 + HTML 雙版本信件，附縮圖 / QA 截圖。"""
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    msg.set_content(text)
    body = html.escape(text).replace("\n", "<br>")
    msg.add_alternative(f"<html><body style='font-family:sans-serif'><h3>{html.escape(subject)}</h3>"
                        f"<p>{body}</p></body></html>", subtype="html")
    _attach_images(msg, attachments)
    return msg


def send_gmail(user: str, app_password: str, to: str, subject: str, text: str,
               attachments: Sequence[Path] = (),
               smtp_factory: Callable[..., Any] = smtplib.SMTP_SSL,
               sleep: Callable[[float], None] = time.sleep) -> bool:
    """Gmail SMTP SSL 465 寄信；認證錯誤不重試，網路錯誤重試；失敗記 log 回傳 False。"""
    try:
        msg = build_email(user, to, subject, text, attachments)
    except (ValueError, TypeError) as exc:
        logger.error("Gmail 信件組裝失敗：%s", exc)
        return False

    def attempt() -> bool:
        try:
            with smtp_factory("smtp.gmail.com", 465, context=ssl.create_default_context(),
                              timeout=NETWORK_TIMEOUT) as smtp:
                smtp.login(user, app_password)
                smtp.send_message(msg)
        except PERMANENT_SMTP_ERRORS as exc:
            # 帳密錯誤 / 收件者被拒重試也沒用（SMTPException 是 OSError 子類別，要先攔）
            logger.error("Gmail 通知失敗（不重試）：%s", exc)
            return False
        return True

    return _with_retry("Gmail 通知", attempt, (OSError,), sleep)


class Notifier:
    """同時送 Gmail + Telegram（能送哪個就送哪個）；dry-run 只印出不送。"""

    def __init__(self, channels: Any, dry_run: bool = False,
                 sleep: Callable[[float], None] = time.sleep):
        self.channels = channels
        self.dry_run = dry_run
        self.sleep = sleep

    def send(self, subject: str, text: str, attachments: Sequence[Path] = ()) -> dict[str, bool]:
        if self.dry_run:
            print(f"[dry-run 通知] {subject}\n{text}\n附件：{[p.name for p in attachments]}")
            return {"telegram": False, "gmail": False}
        results = {"telegram": False, "gmail": False}
        ch = self.channels
        if ch.telegram_bot_token and ch.telegram_chat_id:
            results["telegram"] = send_telegram(ch.telegram_bot_token, ch.telegram_chat_id,
                                                f"{subject}\n\n{text}", sleep=self.sleep)
        if ch.gmail_user and ch.gmail_app_password and ch.notify_email:
            results["gmail"] = send_gmail(ch.gmail_user, ch.gmail_app_password, ch.notify_email,
                                          subject, text, attachments, sleep=self.sleep)
        if not any(results.values()):
            logger.error("所有通知管道都失敗或未設定：%s", subject)
        return results


def emergency_notify(subject: str, text: str, environ: Optional[Mapping[str, str]] = None) -> dict[str, bool]:
    """Config 都載不起來時的最後防線：直接用環境變數裡的憑證發通知。"""
    return Notifier(channels_from_environ(environ)).send(subject, text)


# ---- 訊息格式 ----
def format_taipei(dt: datetime) -> str:
    return dt.astimezone(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M（台北）")


def format_success(ep: int, full_title: str, url: str, publish_dt: datetime,
                   duration: float, qa_summary: str, reused: bool = False) -> tuple[str, str]:
    note = "（沿用既有影片）" if reused else ""
    subject = f"[影片已排程]{note} #{ep:03d} {format_taipei(publish_dt)} 公開"
    text = (f"集數：#{ep:03d}\n標題：{full_title}\n連結：{url}\n"
            f"預定公開：{format_taipei(publish_dt)}\n長度：{duration:.1f} 秒\n{qa_summary}")
    if reused:
        text += "\n\n本集沿用 YouTube 上已存在的影片（依描述標記反查找到），未重新上傳；影片內容為先前上傳的版本。"
    return subject, text


def format_failure(ep: Optional[int], publish_dt: datetime, error_text: str, attempts: int,
                   paused: bool, blackout: bool) -> tuple[str, str]:
    label = f"#{ep:03d}" if ep is not None else "（無集數）"
    prefix = "[此 slot 將開天窗] " if blackout else ""
    subject = f"{prefix}[影片製作失敗] {label} slot {format_taipei(publish_dt)}"
    lines = [f"集數：{label}", f"slot：{format_taipei(publish_dt)}", f"累計失敗：{attempts} 次"]
    if paused:
        lines.append("已達 3 次上限，本集暫停，需要人工處理；之後自動改排下一集。")
    if blackout:
        lines.append("距離公開不到 2 小時，此 slot 很可能開天窗。")
    lines += ["", "錯誤摘要（最後 20 行）：", tail_text(error_text, 20)]
    return subject, "\n".join(lines)
