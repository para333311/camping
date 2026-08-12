"""로그 마스킹.

공개 레포지토리이므로 GitHub Actions 로그에 아이디/비밀번호/토큰/쿠키가
절대 남으면 안 된다. 이 모듈은 두 가지 방식으로 막는다.

1) 환경변수에 들어있는 실제 비밀값을 문자열 치환으로 제거한다.
2) 비밀처럼 생긴 패턴(Set-Cookie, JSESSIONID, Bearer 토큰 등)을 정규식으로 제거한다.

logging 을 거치지 않고 print() 로 새어나가는 것을 막기 위해
setup_logging() 은 sys.stdout / sys.stderr 자체도 감싼다.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import IO, Iterable

MASK = "***"

# 값이 짧으면(예: "1") 온 문서가 *** 로 도배되므로 최소 길이를 둔다.
_MIN_SECRET_LEN = 4

_SECRET_ENV_VARS = (
    "FOREST_ID",
    "FOREST_PW",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # 텔레그램 봇 토큰: 123456789:AAH...
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}"), MASK),
    # Bearer / Authorization 헤더
    (re.compile(r"(?i)\b(authorization|bearer)\b\s*[:=]?\s*\S+"), r"\1 " + MASK),
    # 쿠키 헤더 통째로
    (re.compile(r"(?i)\b(set-cookie|cookie)\b\s*[:=]\s*[^\r\n]+"), r"\1: " + MASK),
    # 세션 쿠키 값
    (re.compile(r"(?i)\b(JSESSIONID|WMONID|SESSION|_csrf|csrfToken|XSRF-TOKEN)\s*=\s*[^;,\s\"']+"),
     r"\1=" + MASK),
    # 비밀번호처럼 보이는 key=value
    (re.compile(r"(?i)\b(password|passwd|pwd|mmberPwd|userPw|forest_pw)\b\s*[:=]\s*[^&\s,;\"']+"),
     r"\1=" + MASK),
    # 아이디처럼 보이는 key=value
    (re.compile(r"(?i)\b(mmberId|userId|loginId|forest_id)\b\s*[:=]\s*[^&\s,;\"']+"),
     r"\1=" + MASK),
    # 텔레그램 API URL 안의 토큰
    (re.compile(r"(?i)(api\.telegram\.org/bot)[^/\s]+"), r"\1" + MASK),
)


def _secret_values() -> list[str]:
    values: list[str] = []
    for name in _SECRET_ENV_VARS:
        raw = os.environ.get(name) or ""
        raw = raw.strip()
        if len(raw) >= _MIN_SECRET_LEN:
            values.append(raw)
            # 토큰의 앞부분(봇 ID)만 새어나가는 경우도 막는다.
            if ":" in raw:
                head = raw.split(":", 1)[1]
                if len(head) >= _MIN_SECRET_LEN:
                    values.append(head)
    # 긴 값부터 지워야 부분 치환으로 조각이 남지 않는다.
    return sorted(set(values), key=len, reverse=True)


def redact(text: str, extra: Iterable[str] = ()) -> str:
    """문자열에서 비밀값과 비밀스러운 패턴을 제거한다."""
    if not text:
        return text
    for value in list(_secret_values()) + [v for v in extra if v]:
        if value and len(value) >= _MIN_SECRET_LEN:
            text = text.replace(value, MASK)
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    """logging 레코드를 내보내기 직전에 마스킹한다."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            record.msg = redact(str(record.getMessage()))
            record.args = ()
        except Exception:  # 로깅이 절대 프로그램을 죽이지 않게 한다.
            record.msg = "(로그 마스킹 실패로 메시지를 감춥니다)"
            record.args = ()
        return True


class _RedactingStream:
    """print() 로 나가는 내용까지 마스킹하는 스트림 래퍼."""

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream

    def write(self, text: str) -> int:
        return self._stream.write(redact(text))

    def flush(self) -> None:
        self._stream.flush()

    def isatty(self) -> bool:
        return getattr(self._stream, "isatty", lambda: False)()

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


def setup_logging(verbose: bool = False) -> logging.Logger:
    """루트 로거를 마스킹 필터와 함께 설정하고 stdout/stderr 도 감싼다."""
    if not isinstance(sys.stdout, _RedactingStream):
        sys.stdout = _RedactingStream(sys.stdout)  # type: ignore[assignment]
    if not isinstance(sys.stderr, _RedactingStream):
        sys.stderr = _RedactingStream(sys.stderr)  # type: ignore[assignment]

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    handler.addFilter(RedactingFilter())

    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    # 라이브러리들이 URL/헤더를 통째로 찍는 것을 막는다.
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger("foresttrip")
