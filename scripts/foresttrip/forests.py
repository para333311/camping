"""휴양림 목록 수집 (HTTP GET + HTML 파싱, 로그인 불필요).

실측 결과(사용자가 실제 HTTP 응답 본문으로 직접 확인, 재조사 금지):
  - 검색 화면은 AJAX 가 아니라 평범한 GET 이고, 서버가 완성된 HTML 을 그대로
    내려준다. 로그인도 필요 없다.
      GET /pot/is/fs/selectFcltSrchView.do?hmpgId=FRIP&menuId=002001&srchArea=<코드>&nowPage=<쪽>
  - srchArea 지역코드: 1=서울/인천/경기, 2=강원, 3=충북, 4=대전/충남, 5=전북,
    6=전남/광주, 7=대구/경북, 8=부산/경남, 9=제주. 수도권은 1 하나로 끝난다.
    (지역명 문자열로 다시 걸러낼 필요가 없다 — 코드 자체가 지역이다)
  - 페이지 수는 하단의 <span class="paging_count">(현재/전체)</span> 에서 읽는다.
    nowPage 에 없는 값을 넣어도 에러 없이 마지막 페이지를 돌려주므로(무한루프
    위험), paging_count 로 읽은 전체 쪽수만큼만 순회한다.
  - 카드 구조:
      <div class="bodo_pt">
        <div class="title">
          <a href="#ID02030023"> ... <b>동두천자연휴양림 </b></a>
      insttId 는 그 a 의 href 에서 '#' 뒤 문자열. 이름은 그 안의 <b> 텍스트.
      (카드 안 공지 링크 등에도 ID 문자열이 나타날 수 있어, 페이지 전체에
      정규식을 돌리지 않고 반드시 div.title 안의 a 만 잡는다)
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
MAX_PAGES_FALLBACK = 10  # paging_count 를 못 읽었을 때만 쓰는 안전 상한

# forests.collect() 가 한 곳도 못 찾았을 때 던지는 예외/텔레그램 메시지.
# 다른 파일(diagnostics.py)이 이 문자열을 그대로 가져다 쓰므로 여기서만 정의한다.
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

# div.title 안의 a[href="#ID..."] 만 잡는다(카드 안 다른 ID 문자열과 헷갈리지 않도록).
_CARD_RE = re.compile(
    r'<div\s+class="title">\s*<a\s+href="#(?P<id>ID\d+)"[^>]*>(?P<inner>.*?)</a>',
    re.DOTALL,
)
_BOLD_RE = re.compile(r"<b[^>]*>(.*?)</b>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_PAGING_RE = re.compile(r'paging_count[^>]*>\s*\(\s*\d+\s*/\s*(\d+)\s*\)')


def _strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text or "").strip()


def _parse_cards(html: str) -> list[dict[str, str]]:
    """div.bodo_pt > div.title > a 에서 (insttId, 이름) 을 뽑는다."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in _CARD_RE.finditer(html):
        instt_id = match.group("id")
        if instt_id in seen:
            continue
        bold = _BOLD_RE.search(match.group("inner"))
        name = _strip_tags(bold.group(1)) if bold else _strip_tags(match.group("inner"))
        if not name:
            continue
        seen.add(instt_id)
        out.append({"insttId": instt_id, "name": name})
    return out


def _parse_total_pages(html: str) -> int | None:
    match = _PAGING_RE.search(html)
    if not match:
        return None
    try:
        return max(1, int(match.group(1)))
    except ValueError:
        return None


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
    """지역코드별로 검색 화면을 GET 하고 카드를 파싱해 휴양림 목록을 모은다."""
    import httpx

    search = endpoints.get("facility_search", {})
    base_url = endpoints.get("base_url", "https://www.foresttrip.go.kr").rstrip("/")
    path = search.get("url", "/pot/is/fs/selectFcltSrchView.do")
    base_params = dict(search.get("base_params", {}))
    region_param = search.get("region_param", "srchArea")
    page_param = search.get("page_param", "nowPage")

    http_cfg = endpoints.get("http", {})
    min_delay = float(http_cfg.get("min_delay_seconds", 0.5))
    max_delay = float(http_cfg.get("max_delay_seconds", 1.0))
    timeout = float(http_cfg.get("timeout_seconds", 20))
    headers = {"User-Agent": http_cfg.get("user_agent", "Mozilla/5.0")}

    by_id: dict[str, dict[str, Any]] = {}
    url = f"{base_url}{path}"

    with httpx.Client(headers=headers, timeout=timeout) as client:
        for code in codes:
            label = REGION_CODE_LABEL.get(code, f"지역코드 {code}")
            params = {**base_params, region_param: code, page_param: 1}
            response = client.get(url, params=params)
            response.raise_for_status()
            html = response.text

            total_pages = _parse_total_pages(html)
            hard_limit = total_pages if total_pages is not None else MAX_PAGES_FALLBACK
            if total_pages is None:
                log.warning(
                    "%s: 전체 쪽수(paging_count)를 못 읽어 최대 %d페이지까지만 봅니다.",
                    label,
                    MAX_PAGES_FALLBACK,
                )

            found_before = len(by_id)
            page_no = 1
            while page_no <= hard_limit:
                if page_no > 1:
                    time.sleep(random.uniform(min_delay, max_delay))
                    params = {**base_params, region_param: code, page_param: page_no}
                    response = client.get(url, params=params)
                    response.raise_for_status()
                    html = response.text

                cards = _parse_cards(html)
                new_on_page = 0
                for card in cards:
                    instt_id = card["insttId"]
                    if instt_id not in by_id:
                        by_id[instt_id] = {"insttId": instt_id, "name": card["name"], "code": code, "region": label}
                        new_on_page += 1

                if new_on_page == 0:
                    break
                page_no += 1

            log.info("%s(코드 %s): 휴양림 %d곳 수집 (%d페이지)", label, code, len(by_id) - found_before, min(page_no, hard_limit))
            time.sleep(random.uniform(min_delay, max_delay))

    forests = sorted(by_id.values(), key=lambda f: (f["region"], f["name"]))
    if not forests:
        raise RuntimeError(FOREST_COLLECT_FAILED_MSG)
    return forests


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
