"""휴양림 목록 수집과 캐싱.

휴양림 이름/insttId 를 코드에 박아두지 않고, 숲나들e 의 휴양림 검색 화면에서
지역 필터로 직접 수집한다. 결과는 config/forests.json 에 캐싱하고 7일마다
다시 수집한다.
"""

from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from . import config

log = logging.getLogger("foresttrip.forests")

CACHE_TTL_DAYS = 7
MAX_PAGES = 12

# 지역 이름이 주소에 어떻게 적혀 있을지 모르므로 별칭을 함께 본다.
REGION_ALIASES: dict[str, tuple[str, ...]] = {
    "서울": ("서울특별시", "서울시", "서울"),
    "인천": ("인천광역시", "인천시", "인천"),
    "경기": ("경기도", "경기"),
    "강원": ("강원특별자치도", "강원도", "강원"),
    "충북": ("충청북도", "충북"),
    "충남": ("충청남도", "충남"),
    "전북": ("전북특별자치도", "전라북도", "전북"),
    "전남": ("전라남도", "전남"),
    "경북": ("경상북도", "경북"),
    "경남": ("경상남도", "경남"),
    "제주": ("제주특별자치도", "제주도", "제주"),
    "대전": ("대전광역시", "대전"),
    "대구": ("대구광역시", "대구"),
    "광주": ("광주광역시", "광주"),
    "울산": ("울산광역시", "울산"),
    "부산": ("부산광역시", "부산"),
    "세종": ("세종특별자치시", "세종"),
}

_EXTRACT_JS = r"""() => {
    const out = {};
    document.querySelectorAll('a[href*="insttId="]').forEach(a => {
        const href = a.getAttribute('href') || '';
        const m = href.match(/insttId=([A-Za-z0-9_-]+)/);
        if (!m) return;
        const id = m[1];
        const box = a.closest('li, tr, .item, .card, .list_item, .srch_item, article') || a.parentElement;
        const name = (a.textContent || '').trim().replace(/\s+/g, ' ');
        const text = ((box && box.innerText) || name || '').trim().replace(/\s+/g, ' ');
        const prev = out[id];
        if (!prev || (prev.name.length < 2 && name.length >= 2) || text.length > prev.text.length) {
            out[id] = { insttId: id, name: name, text: text };
        }
    });
    return Object.values(out);
}"""

_NOISE = re.compile(r"(?:바로가기|자세히보기|예약하기|상세보기|더보기)\s*$")


def _clean_name(raw_name: str, blob: str) -> str:
    name = _NOISE.sub("", (raw_name or "").strip()).strip()
    if len(name) >= 2:
        return name
    # 링크 글자가 비어 있으면 카드 첫 줄을 이름으로 쓴다.
    first_line = (blob or "").split("(")[0].strip()
    return _NOISE.sub("", first_line).strip()[:40]


def _region_of(blob: str, requested: str) -> str | None:
    """카드 텍스트에서 지역을 판별한다. 못 찾으면 None."""
    for region, aliases in REGION_ALIASES.items():
        for alias in aliases:
            if alias in blob:
                return region
    return None


def cache_is_fresh(cache: dict[str, Any], regions: list[str]) -> bool:
    collected_at = cache.get("collected_at")
    if not collected_at or not cache.get("forests"):
        return False
    if sorted(cache.get("regions") or []) != sorted(regions):
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


def collect(session: Any, endpoints: dict[str, Any], regions: list[str]) -> list[dict[str, Any]]:
    """로그인된 브라우저로 지역별 휴양림 목록을 수집한다."""
    search = endpoints.get("facility_search", {})
    base_url = endpoints.get("base_url", "https://www.foresttrip.go.kr").rstrip("/")
    path = search.get("url", "/pot/is/fs/selectFcltSrchView.do")
    base_params = dict(search.get("base_params", {}))
    region_param = search.get("region_param", "srchArea")
    page_param = search.get("page_param", "nowPage")

    http = endpoints.get("http", {})
    min_delay = float(http.get("min_delay_seconds", 0.5))
    max_delay = float(http.get("max_delay_seconds", 1.0))

    page = session.page()
    by_id: dict[str, dict[str, Any]] = {}

    for region in regions:
        found_for_region = 0
        seen_before = set(by_id)

        for page_no in range(1, MAX_PAGES + 1):
            params = {**base_params, region_param: region, page_param: str(page_no)}
            url = f"{base_url}{path}?{urlencode(params, encoding='utf-8')}"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=40_000)
                page.wait_for_timeout(700)
                items = page.evaluate(_EXTRACT_JS) or []
            except Exception as exc:
                log.warning("%s 지역 %d페이지를 읽지 못했습니다: %s", region, page_no, type(exc).__name__)
                break

            new_on_page = 0
            for item in items:
                instt_id = str(item.get("insttId") or "").strip()
                if not instt_id:
                    continue
                blob = str(item.get("text") or "")
                name = _clean_name(str(item.get("name") or ""), blob)
                if not name:
                    continue

                detected = _region_of(blob, region)
                # 지역 필터가 먹지 않는 화면일 수 있으므로 주소로 한 번 더 거른다.
                if detected is not None and detected not in regions:
                    continue

                if instt_id not in by_id:
                    by_id[instt_id] = {
                        "insttId": instt_id,
                        "name": name,
                        "region": detected or region,
                    }
                    new_on_page += 1

            if new_on_page == 0:
                break
            found_for_region += new_on_page
            time.sleep(random.uniform(min_delay, max_delay))

        log.info("%s: 휴양림 %d곳 수집", region, len(set(by_id) - seen_before) or found_for_region)

    forests = sorted(by_id.values(), key=lambda f: (f["region"], f["name"]))
    if not forests:
        raise RuntimeError(
            "휴양림 목록을 한 곳도 수집하지 못했습니다. "
            "숲나들e 검색 화면 구조가 바뀌었을 수 있습니다."
        )
    return forests


def get_forests(session: Any, endpoints: dict[str, Any], regions: list[str]) -> list[dict[str, Any]]:
    """캐시가 살아 있으면 캐시를, 아니면 새로 수집해서 저장 후 돌려준다."""
    cache = config.load_forest_cache()
    if cache_is_fresh(cache, regions):
        forests = cache["forests"]
        log.info("캐시에서 휴양림 %d곳을 불러왔습니다.", len(forests))
        return forests

    log.info("휴양림 목록을 새로 수집합니다. (지역: %s)", ", ".join(regions))
    forests = collect(session, endpoints, regions)
    config.save_forest_cache(
        datetime.now(timezone.utc).isoformat(timespec="seconds"), regions, forests
    )
    return forests
