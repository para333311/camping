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

# forests.collect() 가 한 곳도 못 찾았을 때 던지는 예외 메시지.
# diagnostics.py 가 이 문자열을 보고 원인을 분류하므로, 서로 다른 문구를
# 각자 유지하다 어긋나는 일이 없도록 상수 하나를 두 파일이 함께 쓴다.
FOREST_COLLECT_FAILED_MSG = "휴양림 목록을 한 곳도 수집하지 못했습니다."

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
    const record = (id, name, box) => {
        if (!id) return;
        const text = ((box && box.innerText) || name || '').trim().replace(/\s+/g, ' ');
        const prev = out[id];
        if (!prev || (prev.name.length < 2 && (name || '').length >= 2) || text.length > prev.text.length) {
            out[id] = { insttId: id, name: (name || '').trim().replace(/\s+/g, ' '), text: text };
        }
    };
    const box = a => a.closest('li, tr, .item, .card, .list_item, .srch_item, article') || a.parentElement;

    // 1) href 안에 insttId=... 가 그대로 있는 링크 (가장 흔한 형태)
    document.querySelectorAll('a[href*="insttId="]').forEach(a => {
        const m = (a.getAttribute('href') || '').match(/insttId=([A-Za-z0-9_-]+)/);
        if (m) record(m[1], a.textContent, box(a));
    });

    // 2) onclick="...('0182')..." 처럼 자바스크립트 함수 호출 안에 있는 경우
    document.querySelectorAll('[onclick*="nsttId" i], [onclick*="fclt" i], [onclick*="detail" i]').forEach(el => {
        const m = (el.getAttribute('onclick') || '').match(/['"]([0-9]{3,6})['"]/);
        if (m) record(m[1], el.textContent, box(el));
    });

    // 3) data-instt-id / data-id 같은 데이터 속성을 쓰는 경우
    document.querySelectorAll('[data-instt-id], [data-insttid], [data-instt]').forEach(el => {
        const id = el.getAttribute('data-instt-id') || el.getAttribute('data-insttid') || el.getAttribute('data-instt');
        record(id, el.textContent, box(el));
    });

    return Object.values(out);
}"""

_SEARCH_BUTTON_SELECTORS = (
    "button:has-text('검색')",
    "a:has-text('검색')",
    "button:has-text('조회')",
    "a:has-text('조회')",
    "input[type='submit']",
    "button[type='submit']",
)

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


def _click_search_button(page: Any) -> bool:
    """검색/조회 버튼이 있으면 눌러본다.

    일부 사이트는 주소에 검색어를 넣어 페이지를 열어도 그 자체로는 목록을
    보여주지 않고, 화면 안의 버튼을 눌러야 자바스크립트가 실제 조회를
    실행하는 구조일 수 있다. 그런 경우를 대비한 안전장치다.
    """
    for selector in _SEARCH_BUTTON_SELECTORS:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible():
                locator.click(timeout=5_000)
                page.wait_for_timeout(1_500)
                return True
        except Exception:
            continue
    return False


def _score_forest_rows(rows: list[Any]) -> int:
    """이 배열이 '휴양림 목록'처럼 보이는 정도를 점수로 매긴다."""
    dict_rows = [r for r in rows if isinstance(r, dict)]
    if not dict_rows:
        return 0
    sample = dict_rows[:60]
    keys: set[str] = set()
    for row in sample:
        keys.update(row.keys())
    lowered = {k.lower() for k in keys}

    score = 0
    if any("insttid" in k or "instid" in k for k in lowered):
        score += 50
    if any(k.endswith("nm") or k.endswith("name") for k in lowered):
        score += 20
    if any("addr" in k or "juso" in k for k in lowered):
        score += 10
    score += min(len(dict_rows), 30)
    return score


def _infer_forest_fields(rows: list[dict[str, Any]]) -> tuple[str | None, str | None, str | None]:
    """휴양림 번호/이름/주소가 어느 키에 들어있는지 추론한다."""
    keys: list[str] = []
    for row in rows[:60]:
        for key in row.keys():
            if key not in keys:
                keys.append(key)
    lower = {k.lower(): k for k in keys}

    id_key = next((lower[c] for c in ("insttid", "instid") if c in lower), None)
    name_key = next(
        (lower[c] for c in ("insttnm", "fcltnm", "frstnm", "instnm", "name", "koreannm") if c in lower),
        None,
    )
    if name_key is None:
        name_key = next((k for k in keys if k.lower().endswith("nm") and k != id_key), None)
    addr_key = next((k for k in keys if "addr" in k.lower() or "juso" in k.lower()), None)
    return id_key, name_key, addr_key


def _extract_from_ajax(captured: list[dict[str, Any]]) -> list[dict[str, str]]:
    """이 화면을 여는 동안 오간 JSON 응답에서 휴양림 목록을 찾아본다.

    월별예약조회 주소를 실행 시 관찰해서 알아내는 것과 같은 원리다.
    화면이 서버가 그려준 HTML 이 아니라 자바스크립트로 목록을 그리는
    구조라면, DOM 을 아무리 뒤져도 못 찾고 이 방법으로만 찾을 수 있다.
    """
    from .session import iter_json_arrays

    best_rows: list[dict[str, Any]] = []
    best_score = 0
    for entry in captured:
        for _path, rows in iter_json_arrays(entry.get("payload")):
            score = _score_forest_rows(rows)
            if score > best_score:
                best_score = score
                best_rows = [r for r in rows if isinstance(r, dict)]

    if best_score < 50 or not best_rows:
        return []

    id_key, name_key, addr_key = _infer_forest_fields(best_rows)
    if not id_key or not name_key:
        return []

    out: list[dict[str, str]] = []
    for row in best_rows:
        instt_id = str(row.get(id_key) or "").strip()
        name = str(row.get(name_key) or "").strip()
        if not instt_id or not name:
            continue
        addr = str(row.get(addr_key) or "") if addr_key else ""
        out.append({"insttId": instt_id, "name": name, "text": f"{name} {addr}".strip()})
    return out


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


def _load_page(page: Any, url: str) -> None:
    """화면을 연다. 자바스크립트로 목록을 그리는 화면일 수 있으므로, 페이지
    이동 자체는 빠르게 끝낸 뒤(domcontentloaded) 네트워크가 잠잠해지기를
    별도로 기다린다. 계속 폴링하는 화면이라 잠잠해지지 않아도 재이동하지
    않고(느려지기만 하므로) 그 시점까지 그려진 내용으로 진행한다."""
    page.goto(url, wait_until="domcontentloaded", timeout=20_000)
    try:
        page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass
    page.wait_for_timeout(1_200)


def _extract_items(page: Any, session: Any, mode: str | None) -> tuple[list[dict[str, str]], str, str]:
    """DOM 에서 먼저 찾아보고, 못 찾으면 검색 버튼을 눌러보고, 그래도 못 찾으면
    오간 JSON 응답을 뒤진다. mode 가 이미 정해져 있으면(이전 페이지에서
    성공한 방법) 그 방법만 다시 쓴다.

    돌려주는 값: (찾은 항목들, 다음 페이지에도 쓸 방법 코드('dom'/'ajax'),
    사람이 읽을 설명)
    """
    if mode in (None, "dom"):
        items = page.evaluate(_EXTRACT_JS) or []
        if items:
            return items, "dom", "DOM 링크/속성에서 바로 찾음"
        if mode is None and _click_search_button(page):
            items = page.evaluate(_EXTRACT_JS) or []
            if items:
                return items, "dom", "검색 버튼을 누른 뒤 DOM 에서 찾음"

    if mode in (None, "ajax"):
        items = _extract_from_ajax(session.captured)
        if items:
            return items, "ajax", "페이지가 불러온 JSON 응답에서 찾음"

    return [], mode or "", "0건"


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
    attempts: list[str] = []  # 실패했을 때 진단 메시지에 쓸, 지역별로 무엇을 시도했는지 기록

    for region in regions:
        seen_before = set(by_id)
        mode: str | None = None
        last_desc = "0건"

        for page_no in range(1, MAX_PAGES + 1):
            params = {**base_params, region_param: region, page_param: str(page_no)}
            url = f"{base_url}{path}?{urlencode(params, encoding='utf-8')}"

            session.captured.clear()
            try:
                _load_page(page, url)
            except Exception as exc:
                log.warning("%s 지역 %d페이지를 읽지 못했습니다: %s", region, page_no, type(exc).__name__)
                break

            items, used_mode, desc = _extract_items(page, session, mode)
            if page_no == 1:
                attempts.append(f"{region}: {desc}")
                last_desc = desc
            if items:
                mode = used_mode

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
            time.sleep(random.uniform(min_delay, max_delay))

        log.info("%s: 휴양림 %d곳 수집 (%s)", region, len(set(by_id) - seen_before), last_desc)

    forests = sorted(by_id.values(), key=lambda f: (f["region"], f["name"]))
    if not forests:
        raise RuntimeError(
            f"{FOREST_COLLECT_FAILED_MSG} 시도한 방법: {'; '.join(attempts) or '없음'}. "
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
