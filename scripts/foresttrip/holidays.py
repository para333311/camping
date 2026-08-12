"""휴양림별 휴장일 / 최대 연박수 조회 (JSON API, 로그인 불필요, 실측 확정).

GET /rep/or/selectFcFsRcfrsFcltInfo.do?insttId=<휴양림ID>
응답: {
  "rcfrsFcltInfo": [{"mxmmStngDayCnt": 3, ...}],
  "hldtList": [{"dt": "20260818", "dtNm": "8월 17일 개장 대체 휴장"}, ...],
  "useDtList": [{"schdlDt": "20260812"}, ...]   # 예약이 열려있는 날짜(약 41일)
}

용도는 딱 세 가지 뿐이다.
  (a) 조회 대상 날짜가 useDtList(예약 오픈 범위) 밖이면 그 휴양림 결과는 뺀다.
  (b) hldtList 의 날짜는 휴장일이므로 결과에서 뺀다.
  (c) mxmmStngDayCnt(최대 연박수)를 넘는 연박 묶음은 그 값만큼씩 잘라서 알린다.
useDtList 는 빈자리 여부가 아니라 '예약 오픈 여부'이므로 그 자체로 빈자리
판단에 쓰지 않는다.

1시간 캐시. 캐시는 다른 캐시들과 같은 방식으로 config/ 아래 파일에
저장하고, 워크플로가 바뀐 게 있을 때만 커밋한다.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import config

log = logging.getLogger("foresttrip.holidays")

CACHE_TTL_HOURS = 1


def _parse_yyyymmdd(value: Any) -> date | None:
    text = str(value or "").strip()
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def _fetch_one(client: Any, base_url: str, path: str, param_name: str, instt_id: str) -> dict[str, Any]:
    response = client.get(f"{base_url}{path}", params={param_name: instt_id})
    response.raise_for_status()
    data = response.json()

    max_nights: int | None = None
    info_list = data.get("rcfrsFcltInfo") or []
    if info_list and isinstance(info_list[0], dict):
        raw = info_list[0].get("mxmmStngDayCnt")
        try:
            max_nights = int(raw)
        except (TypeError, ValueError):
            max_nights = None

    closed: list[str] = []
    for item in data.get("hldtList") or []:
        if not isinstance(item, dict):
            continue
        d = _parse_yyyymmdd(item.get("dt"))
        if d:
            closed.append(d.isoformat())

    open_dates: list[str] = []
    for item in data.get("useDtList") or []:
        if not isinstance(item, dict):
            continue
        d = _parse_yyyymmdd(item.get("schdlDt"))
        if d:
            open_dates.append(d.isoformat())

    return {"maxNights": max_nights, "closed": sorted(set(closed)), "open": sorted(set(open_dates))}


def cache_is_fresh(cache: dict[str, Any], forest_ids: list[str]) -> bool:
    collected_at = cache.get("collected_at")
    info = cache.get("info") or {}
    if not collected_at or not info:
        return False
    if sorted(info.keys()) != sorted(forest_ids):
        return False
    try:
        stamp = datetime.fromisoformat(str(collected_at))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp) <= timedelta(hours=CACHE_TTL_HOURS)


def get_all(endpoints: dict[str, Any], forests: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{insttId: {"maxNights": int|None, "closed": [iso...], "open": [iso...]}} 를 돌려준다."""
    import httpx

    forest_ids = sorted({str(f["insttId"]) for f in forests})

    cache = config.load_holiday_cache()
    if cache_is_fresh(cache, forest_ids):
        log.info("캐시에서 휴양림별 휴장일/최대연박수 정보를 불러왔습니다. (%d곳)", len(forest_ids))
        return cache["info"]

    cfg = endpoints.get("fclt_info", {})
    base_url = endpoints.get("base_url", "https://www.foresttrip.go.kr").rstrip("/")
    path = cfg.get("url", "/rep/or/selectFcFsRcfrsFcltInfo.do")
    param_name = cfg.get("param_name", "insttId")

    http_cfg = endpoints.get("http", {})
    min_delay = float(http_cfg.get("min_delay_seconds", 0.5))
    max_delay = float(http_cfg.get("max_delay_seconds", 1.0))
    timeout = float(http_cfg.get("timeout_seconds", 20))
    headers = {"User-Agent": http_cfg.get("user_agent", "Mozilla/5.0")}

    info: dict[str, Any] = {}
    with httpx.Client(headers=headers, timeout=timeout) as client:
        for i, instt_id in enumerate(forest_ids):
            if i:
                time.sleep(random.uniform(min_delay, max_delay))
            try:
                info[instt_id] = _fetch_one(client, base_url, path, param_name, instt_id)
            except Exception as exc:
                log.warning("%s 휴장일/최대연박수 정보를 가져오지 못했습니다: %s", instt_id, type(exc).__name__)

    if info:
        # 전부 실패했다면(사이트 점검 등) '방금 막 수집한 빈 캐시' 를 저장하지
        # 않는다 — 저장해버리면 다음 실행이 이걸 최신 캐시로 오인해 그대로
        # 쓰거나, 실제로는 아무것도 못 가져왔는데 커밋 기록만 남는다.
        config.save_holiday_cache(datetime.now(timezone.utc).isoformat(timespec="seconds"), info)
    log.info("휴양림 %d/%d곳의 휴장일/최대연박수 정보를 새로 수집했습니다.", len(info), len(forest_ids))
    return info
