"""조회할 숙박일(체크인 날짜) 계산.

규칙 (config/dates.json 이 유일한 입력):
  (가) holidays 에 적힌 연휴 숙박일
  (나) 오늘부터 weeks_ahead 주 이내의 금요일박 / 토요일박
  (다) 둘을 합치고 중복 제거, 오늘 이전 날짜는 제외
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Iterable

log = logging.getLogger("foresttrip.dateplan")


def _parse_iso(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value).strip())
    except (ValueError, AttributeError):
        log.warning("config/dates.json 의 날짜 형식을 읽을 수 없어 건너뜁니다: %r", value)
        return None


def holiday_nights(cfg: dict[str, Any]) -> list[date]:
    out: list[date] = []
    for block in cfg.get("holidays", []) or []:
        if not isinstance(block, dict):
            continue
        for raw in block.get("nights", []) or []:
            parsed = _parse_iso(raw)
            if parsed:
                out.append(parsed)
    return out


def weekend_nights(cfg: dict[str, Any], today: date) -> list[date]:
    weekend = cfg.get("weekend") or {}
    if not weekend.get("enabled", True):
        return []

    weeks_ahead = int(weekend.get("weeks_ahead", 8))
    weekdays = {int(w) for w in weekend.get("weekdays", [4, 5])}
    if not weekdays:
        return []

    horizon = today + timedelta(weeks=max(weeks_ahead, 0))
    out: list[date] = []
    cursor = today
    while cursor <= horizon:
        if cursor.weekday() in weekdays:
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


def build_target_dates(cfg: dict[str, Any], today: date) -> list[date]:
    """조회 대상 숙박일을 정렬된 중복 없는 목록으로 돌려준다."""
    combined = set(holiday_nights(cfg)) | set(weekend_nights(cfg, today))
    # (다) 오늘 이전 날짜 제외. 오늘 밤(당일 숙박)은 남긴다.
    upcoming = sorted(d for d in combined if d >= today)

    dropped = len(combined) - len(upcoming)
    if dropped:
        log.info("이미 지난 날짜 %d개를 제외했습니다.", dropped)
    log.info(
        "조회 대상 숙박일 %d개 (%s ~ %s)",
        len(upcoming),
        upcoming[0].isoformat() if upcoming else "-",
        upcoming[-1].isoformat() if upcoming else "-",
    )
    return upcoming


def month_keys(dates: Iterable[date]) -> list[str]:
    """월별예약조회는 '월' 단위로 부르므로 필요한 YYYYMM 목록을 뽑는다."""
    return sorted({f"{d.year:04d}{d.month:02d}" for d in dates})


def group_consecutive(dates: Iterable[date]) -> list[list[date]]:
    """정렬된 날짜들을 연속 구간으로 묶는다. [8/15, 8/16, 8/18] -> [[8/15, 8/16], [8/18]]"""
    ordered = sorted(set(dates))
    if not ordered:
        return []
    runs: list[list[date]] = [[ordered[0]]]
    for current in ordered[1:]:
        if current - runs[-1][-1] == timedelta(days=1):
            runs[-1].append(current)
        else:
            runs.append([current])
    return runs
