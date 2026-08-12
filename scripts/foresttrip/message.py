"""텔레그램 메시지 조립 (HTML parse_mode)."""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any

from . import deeplink
from .query import Opening, QueryReport

MAX_ITEMS = 30


def _fmt_md(value: Any) -> str:
    """8/15 처럼 앞의 0 을 뗀 월/일."""
    return f"{value.month}/{value.day}"


def stay_label(opening: Opening) -> str:
    """'8/15~8/17 (2박)' 또는 '8/16 (1박)'."""
    if opening.nights == 1:
        return f"{_fmt_md(opening.start)} (1박)"
    return f"{_fmt_md(opening.start)}~{_fmt_md(opening.end)} ({opening.nights}박)"


def _line(opening: Opening, endpoints: dict[str, Any]) -> str:
    url = deeplink.build(endpoints, opening.instt_id, opening.forest_name, opening.arcd)
    parts = [
        escape(opening.forest_name),
        escape(opening.goods_name),
        f"<b>{escape(stay_label(opening))}</b>",
    ]
    if opening.capacity:
        parts.append(f"{escape(str(opening.capacity))}인")
    inner = " / ".join(parts)
    return f'✅ <a href="{escape(url, quote=True)}">{inner}</a>'


def build_message(
    report: QueryReport,
    endpoints: dict[str, Any],
    now: datetime,
    label: str,
    heartbeat: bool,
) -> str:
    header = f"🌲 <b>{now.strftime('%m/%d %H:00')} {escape(label)} 휴양림</b>"
    lines = [header]

    if report.openings:
        shown = report.openings[:MAX_ITEMS]
        lines.extend(_line(o, endpoints) for o in shown)
        extra = len(report.openings) - len(shown)
        if extra > 0:
            lines.append(f"… 외 {extra}건")
    else:
        lines.append("예약 가능 객실 없음")

    lines.append(f"(조회 {report.checked}곳 / 실패 {report.failed}곳)")

    if heartbeat:
        lines.append(f"🟢 정상 작동 중 (마지막 점검 {now.strftime('%m/%d %H:00')})")

    return "\n".join(lines)


def build_failure_message(reason: str, now: datetime) -> str:
    return (
        f"🌲 <b>{now.strftime('%m/%d %H:00')} 휴양림 알림</b>\n"
        f"⚠️ 조회 실패: {escape(str(reason))}"
    )
