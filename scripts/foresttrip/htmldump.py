"""DUMP_HTML=1 일 때, 결과 페이지 HTML 중 결과 목록으로 추정되는 영역을
잘라 텔레그램으로 보낼 수 있게 준비한다.

셀렉터를 추측해서 코드에 박지 않기 위한 도구다 — 실제 셀렉터는 이 발췌를
사람이 보고 config/endpoints.json 의 result_selectors 에 채워 넣는다.
"""

from __future__ import annotations

import re
from html import escape

_LANDMARK_KEYWORDS = ("객실", "정원", "예약가능", "잔여", "상품", "휴양관", "숲속의집")
_BODY_RE = re.compile(r"<body[^>]*>(.*)</body>", re.DOTALL | re.IGNORECASE)

SNIPPET_LEN = 3000
WINDOW_BEFORE = 1500


def extract_snippet(html: str) -> str:
    """결과 목록 추정 영역 앞뒤 포함 3000자를 잘라낸다."""
    match = _BODY_RE.search(html or "")
    body = match.group(1) if match else (html or "")

    pos = None
    for keyword in _LANDMARK_KEYWORDS:
        idx = body.find(keyword)
        if idx != -1:
            pos = idx
            break

    if pos is None:
        return body[:SNIPPET_LEN]

    start = max(0, pos - WINDOW_BEFORE)
    return body[start : start + SNIPPET_LEN]


def split_for_telegram(snippet: str, parts: int = 4) -> list[str]:
    """3~4개 메시지로 나눈다. 각 조각은 <pre> 로 감싸 그대로 읽을 수 있게 한다."""
    if not snippet:
        return ["(빈 화면입니다)"]

    chunk_len = max(1, -(-len(snippet) // parts))  # 올림 나눗셈
    chunks = [snippet[i : i + chunk_len] for i in range(0, len(snippet), chunk_len)]

    total = len(chunks)
    return [
        f"📋 HTML 발췌 {i + 1}/{total}\n<pre>{escape(chunk)}</pre>"
        for i, chunk in enumerate(chunks)
    ]
