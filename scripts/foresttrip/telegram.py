"""텔레그램 발송.

- chat_id 는 "-1001234567890" 같은 숫자도, "@채널주소" 문자열도 그대로 동작한다.
- 4096자를 넘으면 줄 단위로 잘라 여러 번 보낸다.
- 빈자리가 있을 때만 소리 알림을 켜고, 없을 때는 무음으로 보낸다.
"""

from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger("foresttrip.telegram")

API = "https://api.telegram.org/bot{token}/sendMessage"
LIMIT = 4096
SAFE_LIMIT = 3900  # 여유를 둬서 자른다.


class TelegramError(RuntimeError):
    pass


def normalize_chat_id(raw: str) -> str:
    """숫자 chat_id 와 @채널주소 를 모두 받아 그대로 쓸 수 있게 다듬는다."""
    value = (raw or "").strip().strip('"').strip("'")
    if not value:
        raise TelegramError("TELEGRAM_CHAT_ID 가 비어 있습니다.")
    if value.startswith("@"):
        return value
    # -100... 또는 숫자
    if value.lstrip("-").isdigit():
        return value
    # 사용자가 @ 를 빠뜨린 채널 주소로 본다.
    return "@" + value


def split_message(text: str, limit: int = SAFE_LIMIT) -> list[str]:
    """줄 단위로 4096자 제한에 맞춰 나눈다."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in text.split("\n"):
        # 한 줄 자체가 너무 길면 통째로 자른다(드문 경우).
        while len(line) > limit:
            if current:
                chunks.append("\n".join(current))
                current, length = [], 0
            chunks.append(line[:limit])
            line = line[limit:]

        addition = len(line) + (1 if current else 0)
        if length + addition > limit:
            chunks.append("\n".join(current))
            current, length = [line], len(line)
        else:
            current.append(line)
            length += addition

    if current:
        chunks.append("\n".join(current))
    return chunks


def send(token: str, chat_id: str, text: str, silent: bool) -> None:
    """메시지를 보낸다. 길면 나눠서 순서대로 보낸다."""
    import httpx

    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN 이 비어 있습니다.")

    target = normalize_chat_id(chat_id)
    chunks = split_message(text)
    url = API.format(token=token)

    with httpx.Client(timeout=20) as client:
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": target,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                # 빈자리가 있을 때만 소리가 울린다.
                "disable_notification": silent,
            }
            last_error = ""
            for attempt in range(3):
                try:
                    response = client.post(url, json=payload)
                    if response.status_code == 429:
                        retry_after = 3
                        try:
                            retry_after = int(response.json()["parameters"]["retry_after"])
                        except Exception:
                            pass
                        log.warning("텔레그램 속도 제한. %d초 후 재시도합니다.", retry_after)
                        time.sleep(min(retry_after, 30))
                        continue
                    if response.status_code >= 400:
                        # 응답 본문에는 토큰이 없지만, 혹시 몰라 코드/설명만 남긴다.
                        try:
                            last_error = str(response.json().get("description", ""))
                        except Exception:
                            last_error = f"HTTP {response.status_code}"
                        raise TelegramError(f"텔레그램 발송 실패: {last_error}")
                    break
                except TelegramError:
                    raise
                except Exception as exc:
                    last_error = type(exc).__name__
                    if attempt == 2:
                        raise TelegramError(f"텔레그램 발송 실패: {last_error}") from exc
                    time.sleep(2 ** attempt)

            if len(chunks) > 1 and index < len(chunks) - 1:
                time.sleep(0.5)

    log.info("텔레그램 발송 완료 (%d개 메시지, 무음=%s)", len(chunks), silent)
