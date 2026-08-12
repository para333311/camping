"""휴양림 목록 수집 (JSON API, 로그인 불필요, 실측 확정 — 재조사 금지).

GET /rep/or/selectInsttListForSearch.do?srchSido=<코드>
응답: {"insttList": [{"insttId": "...", "insttNm": "...", "insttTpCd": "01|02|04"}, ...]}

실측 주의사항: srchSido 값에 앞자리 0 을 붙이면(예: "01") insttList 가
빈 배열로 온다. 반드시 "1" 처럼 그대로 보낸다. 페이지네이션은 없다.

지역코드: 1=서울/인천/경기, 2=강원, 3=충북, 4=대전/충남, 5=전북,
6=전남/광주, 7=대구/경북, 8=부산/경남, 9=제주.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config

log = logging.getLogger("foresttrip.forests")

CACHE_TTL_DAYS = 7

FOREST_COLLECT_FAILED_MSG = "휴양림 목록 수집 실패 (0곳)"

REGION_CODE_LABEL: dict[int, str] = {
    1: "서울/인천/경기",
    2: "강원",
    3: "충북",
    4: "대전/충남",
    5: "전북",
    6: "전남/광주",
    7: "대구/경북",
    8: "부산/경남",
    9: "제주",
}


def cache_is_fresh(cache: dict[str, Any], codes: list[int]) -> bool:
    collected_at = cache.get("collected_at")
    if not collected_at or not cache.get("forests"):
        return False
    if sorted(cache.get("codes") or []) != sorted(codes):
        log.info("조회 지역이 바뀌어 휴양림 목록을 다시 수집합니다.")
        return False
    try:
        stamp = datetime.fromisoformat(str(collected_at))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - stamp
    if age > timedelta(days=CACHE_TTL_DAYS):
        log.info("휴양림 목록 캐시가 %d일 지나 다시 수집합니다.", age.days)
        return False
    return True


def collect(endpoints: dict[str, Any], codes: list[int]) -> list[dict[str, Any]]:
    """지역코드별로 JSON API 를 호출해 휴양림 목록을 모은다."""
    import httpx

    cfg = endpoints.get("facility_json", {})
    base_url = endpoints.get("base_url", "https://www.foresttrip.go.kr").rstrip("/")
    path = cfg.get("url", "/rep/or/selectInsttListForSearch.do")
    param_name = cfg.get("param_name", "srchSido")

    http_cfg = endpoints.get("http", {})
    timeout = float(http_cfg.get("timeout_seconds", 20))
    headers = {"User-Agent": http_cfg.get("user_agent", "Mozilla/5.0")}

    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    with httpx.Client(headers=headers, timeout=timeout) as client:
        for code in codes:
            label = REGION_CODE_LABEL.get(code, f"지역코드 {code}")
            # 실측 주의: 앞자리 0 을 붙이면("01") 빈 배열이 온다. 그대로 보낸다.
            response = client.get(f"{base_url}{path}", params={param_name: str(code)})
            response.raise_for_status()
            data = response.json()
            rows = data.get("insttList") or []

            added = 0
            for row in rows:
                if not isinstance(row, dict):
                    continue
                instt_id = str(row.get("insttId") or "").strip()
                name = str(row.get("insttNm") or "").strip()
                if not instt_id or not name or instt_id in seen:
                    continue
                seen.add(instt_id)
                out.append(
                    {
                        "insttId": instt_id,
                        "name": name,
                        "code": code,
                        "region": label,
                        "insttTpCd": row.get("insttTpCd"),
                    }
                )
                added += 1
            log.info("%s(코드 %s): 휴양림 %d곳 수집", label, code, added)

    if not out:
        raise RuntimeError(FOREST_COLLECT_FAILED_MSG)
    return sorted(out, key=lambda f: (f["region"], f["name"]))


def get_forests(endpoints: dict[str, Any], codes: list[int]) -> list[dict[str, Any]]:
    """캐시가 살아 있으면 캐시를, 아니면 새로 수집해서 저장 후 돌려준다."""
    cache = config.load_forest_cache()
    if cache_is_fresh(cache, codes):
        forests = cache["forests"]
        log.info("캐시에서 휴양림 %d곳을 불러왔습니다.", len(forests))
        return forests

    log.info("휴양림 목록을 새로 수집합니다. (지역코드: %s)", ", ".join(str(c) for c in codes))
    forests = collect(endpoints, codes)
    config.save_forest_cache(datetime.now(timezone.utc).isoformat(timespec="seconds"), codes, forests)
    return forests
