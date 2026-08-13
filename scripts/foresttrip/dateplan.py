"""조회할 숙박일(체크인 날짜) 계산.

규칙 (config/dates.json 이 유일한 입력):

    숙박일 D 는 'D 도 쉬는 날' 이고 'D+1 도 쉬는 날' 일 때만 대상이다.

    ① D 가 쉬는 날이어야 한다 — 가는 날에도 쉬어야 갈 수 있다.
       금요일 퇴근 후 출발은 불가능하므로 평범한 금요일박은 빠진다.
    ② D+1 도 쉬는 날이어야 한다 — 다음날 출근이 없어야 한다.
       그래서 평범한 일요일박도 빠진다.

    쉬는 날 = 토/일 + config 의 public_holidays.

결과적으로
  - 평범한 주      : 토요일박만
  - 토·일·월 연휴  : 토요일박 + 일요일박
  - 금·토·일 연휴  : 금요일박 + 토요일박
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Iterable

log = logging.getLogger("foresttrip.dateplan")

DEFAULT_WEEKEND_DAYS = (5, 6)  # 토, 일


def _parse_iso(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value).strip())
    except (ValueError, AttributeError):
        log.warning("config/dates.json 의 날짜 형식을 읽을 수 없어 건너뜁니다: %r", value)
        return None


def public_holidays(cfg: dict[str, Any]) -> set[date]:
    out: set[date] = set()
    for raw in cfg.get("public_holidays", []) or []:
        parsed = _parse_iso(raw)
        if parsed:
            out.add(parsed)
    return out


def is_day_off(day: date, holidays: set[date], weekend_days: set[int]) -> bool:
    """그 날 쉬는가? (주말이거나 공휴일이면 쉰다)"""
    return day.weekday() in weekend_days or day in holidays


def build_target_dates(cfg: dict[str, Any], today: date) -> list[date]:
    """조회 대상 숙박일을 정렬된 중복 없는 목록으로 돌려준다."""
    holidays = public_holidays(cfg)
    weekend_days = {int(w) for w in cfg.get("weekend_days", DEFAULT_WEEKEND_DAYS)}
    weeks_ahead = int(cfg.get("weeks_ahead", 8))

    horizon = today + timedelta(weeks=max(weeks_ahead, 0))
    # 조회 범위보다 뒤에 있는 연휴도 놓치지 않도록, 적어둔 공휴일까지는 늘려서 본다.
    if holidays:
        horizon = max(horizon, max(holidays) + timedelta(days=1))

    out: list[date] = []
    cursor = today
    while cursor <= horizon:
        if is_day_off(cursor, holidays, weekend_days) and is_day_off(
            cursor + timedelta(days=1), holidays, weekend_days
        ):
            out.append(cursor)
        cursor += timedelta(days=1)

    log.info(
        "조회 대상 숙박일 %d개 (%s ~ %s)",
        len(out),
        out[0].isoformat() if out else "-",
        out[-1].isoformat() if out else "-",
    )
    return out


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
