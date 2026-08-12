"""월별예약조회 호출, 필터링, 연박 묶기.

로그인은 Playwright 가 한 번만 하고, 실제 조회는 여기서 가벼운 httpx 요청으로
처리한다. 서버에 부담을 주지 않도록 동시 요청 3개, 요청 사이 0.5~1초 간격을
지킨다.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

from .dateplan import group_consecutive

log = logging.getLogger("foresttrip.query")

EXCLUDE_KEYWORD = "예비"  # 운영자 보유분이라 제외


class AuthExpired(RuntimeError):
    """세션이 끊겨 401 이나 안내 HTML 이 돌아온 경우."""


@dataclass
class Slot:
    """객실 하루치 빈자리."""

    instt_id: str
    forest_name: str
    goods_name: str
    use_date: date
    arcd: Any = None  # 딥링크에 쓰는 지역코드(srchInsttArcd)
    capacity: str | None = None
    goods_id: str | None = None

    def key(self) -> tuple[str, date, str]:
        return (self.instt_id, self.use_date, self.goods_name)


@dataclass
class Opening:
    """연박으로 묶은 최종 알림 단위."""

    instt_id: str
    forest_name: str
    goods_name: str
    start: date
    end: date          # 체크아웃 날짜 (마지막 숙박일 + 1)
    nights: int
    arcd: Any = None
    capacity: str | None = None
    goods_id: str | None = None
    url: str = ""


@dataclass
class QueryReport:
    openings: list[Opening] = field(default_factory=list)
    checked: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 응답 파싱
# --------------------------------------------------------------------------- #

def extract_rows(payload: Any, rows_path: str | None) -> list[dict[str, Any]]:
    """rows_path 를 따라가 객실 목록 배열을 꺼낸다. 실패하면 자동 탐색."""
    node: Any = payload
    if rows_path:
        for part in rows_path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif isinstance(node, list) and node:
                node = node[0]
                if isinstance(node, dict) and part in node:
                    node = node[part]
            else:
                node = None
                break
    if isinstance(node, list) and any(isinstance(r, dict) for r in node):
        return [r for r in node if isinstance(r, dict)]

    from .session import analyse_payload

    _path, rows, score = analyse_payload(payload)
    return rows if score >= 40 else []


def _parse_row_date(value: Any) -> date | None:
    if value is None:
        return None
    text = str(value).strip().replace("-", "").replace("/", "").replace(".", "")
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def _is_available(row: dict[str, Any], field_map: dict[str, Any]) -> bool:
    remain_key = field_map.get("remain")
    if remain_key and remain_key in row:
        raw = str(row.get(remain_key) or "").strip()
        if raw.lstrip("-").isdigit():
            return int(raw) > 0

    flag_key = field_map.get("available_flag")
    if flag_key and flag_key in row:
        raw = row.get(flag_key)
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().upper() in {"Y", "1", "TRUE", "가능"}

    # 잔여수/가능여부 필드를 못 찾았으면, 응답에 그 행이 있다는 것 자체를
    # 예약 가능으로 본다. (월별예약조회는 예약 가능한 객실만 내려주는 화면)
    return True


_mismatch_warned = False


def rows_to_slots(
    rows: Iterable[dict[str, Any]],
    forest: dict[str, Any],
    field_map: dict[str, Any],
    wanted: set[date],
) -> list[Slot]:
    global _mismatch_warned

    date_key = field_map.get("use_date") or "useDt"
    name_key = field_map.get("goods_name") or "goodsNm"
    cap_key = field_map.get("capacity")
    goods_key = field_map.get("goods_id")
    instt_key = field_map.get("instt_id")
    forest_instt = str(forest["insttId"])

    slots: list[Slot] = []
    for row in rows:
        # 안전장치: 휴양림 파라미터 이름을 잘못 알아낸 경우 엉뚱한 휴양림의
        # 객실이 섞여 들어올 수 있다. 응답에 휴양림 번호가 있으면 대조한다.
        if instt_key and row.get(instt_key) not in (None, ""):
            if str(row.get(instt_key)).strip() != forest_instt:
                if not _mismatch_warned:
                    _mismatch_warned = True
                    log.warning(
                        "요청한 휴양림(%s)과 다른 휴양림의 응답이 섞여 있어 걸러냅니다. "
                        "config/endpoints.json 의 monthly.param_names.instt 를 확인하세요.",
                        forest_instt,
                    )
                continue

        use_date = _parse_row_date(row.get(date_key))
        if use_date is None or use_date not in wanted:
            # 요청 범위 밖 날짜(API 가 5일 윈도우를 함께 주는 경우)는 버린다.
            continue

        goods_name = str(row.get(name_key) or "").strip()
        if not goods_name or EXCLUDE_KEYWORD in goods_name:
            continue

        if not _is_available(row, field_map):
            continue

        capacity = None
        if cap_key and row.get(cap_key) not in (None, ""):
            capacity = str(row.get(cap_key)).strip()

        slots.append(
            Slot(
                instt_id=str(forest["insttId"]),
                forest_name=str(forest["name"]),
                goods_name=goods_name,
                use_date=use_date,
                arcd=forest.get("code"),
                capacity=capacity,
                goods_id=str(row.get(goods_key)) if goods_key and row.get(goods_key) else None,
            )
        )
    return slots


def dedup(slots: Iterable[Slot]) -> list[Slot]:
    seen: set[tuple[str, date, str]] = set()
    out: list[Slot] = []
    for slot in slots:
        if slot.key() in seen:
            continue
        seen.add(slot.key())
        out.append(slot)
    return out


def build_openings(slots: Iterable[Slot]) -> list[Opening]:
    """같은 (휴양림, 객실)의 빈 날짜를 연속 구간으로 묶는다."""
    from collections import defaultdict
    from datetime import timedelta

    buckets: dict[tuple[str, str], list[Slot]] = defaultdict(list)
    for slot in slots:
        buckets[(slot.instt_id, slot.goods_name)].append(slot)

    openings: list[Opening] = []
    for (instt_id, goods_name), group in buckets.items():
        by_date = {s.use_date: s for s in group}
        for run in group_consecutive(by_date.keys()):
            first = by_date[run[0]]
            openings.append(
                Opening(
                    instt_id=instt_id,
                    forest_name=first.forest_name,
                    goods_name=goods_name,
                    start=run[0],
                    end=run[-1] + timedelta(days=1),
                    nights=len(run),
                    arcd=first.arcd,
                    capacity=first.capacity,
                    goods_id=first.goods_id,
                )
            )

    # 정렬: 박수 많은 순 -> 날짜 빠른 순 -> 휴양림명
    openings.sort(key=lambda o: (-o.nights, o.start, o.forest_name, o.goods_name))
    return openings


# --------------------------------------------------------------------------- #
# HTTP 조회
# --------------------------------------------------------------------------- #

def _month_bounds(year: int, month: int) -> tuple[date, date]:
    import calendar

    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def _build_request(
    monthly: dict[str, Any],
    instt_id: str,
    date_from: date,
    date_to: date,
) -> tuple[str, dict[str, str]]:
    names = monthly.get("param_names", {})
    params: dict[str, str] = {str(k): str(v) for k, v in (monthly.get("params") or {}).items()}

    if names.get("instt"):
        params[names["instt"]] = instt_id
    if names.get("ym"):
        params[names["ym"]] = f"{date_from.year:04d}{date_from.month:02d}"
    if names.get("date_from"):
        params[names["date_from"]] = date_from.strftime("%Y%m%d")
    if names.get("date_to"):
        params[names["date_to"]] = date_to.strftime("%Y%m%d")

    return monthly.get("method", "GET").upper(), params


async def _fetch(
    client: Any,
    url: str,
    method: str,
    params: dict[str, str],
) -> Any:
    if method == "POST":
        response = await client.post(url, data=params)
    else:
        response = await client.get(url, params=params)

    if response.status_code in (401, 403):
        raise AuthExpired(f"HTTP {response.status_code}")

    ctype = response.headers.get("content-type", "").lower()
    if "json" not in ctype:
        # 로그인이 풀리면 JSON 대신 안내 HTML 이 돌아온다.
        raise AuthExpired("JSON 이 아닌 응답(안내 HTML)을 받았습니다")

    try:
        return response.json()
    except Exception as exc:
        raise AuthExpired(f"JSON 파싱 실패: {type(exc).__name__}") from exc


async def _query_forest(
    client: Any,
    forest: dict[str, Any],
    endpoints: dict[str, Any],
    targets: list[date],
    semaphore: asyncio.Semaphore,
    pacer: "Pacer",
) -> list[Slot]:
    monthly = endpoints.get("monthly", {})
    field_map = endpoints.get("field_map", {})
    base_url = endpoints.get("base_url", "").rstrip("/")
    url = base_url + str(monthly.get("url") or "")
    wanted = set(targets)

    months = sorted({(d.year, d.month) for d in targets})
    slots: list[Slot] = []

    for year, month in months:
        first, last = _month_bounds(year, month)
        method, params = _build_request(monthly, str(forest["insttId"]), first, last)

        async with semaphore:
            await pacer.wait()
            payload = await _fetch(client, url, method, params)

        rows = extract_rows(payload, monthly.get("rows_path"))
        got = rows_to_slots(rows, forest, field_map, wanted)
        slots.extend(got)

        # 응답이 특정 날짜만 담고 있는 형태(짧은 윈도우)라면 빠진 날짜를 따로 채운다.
        covered = {s.use_date for s in got}
        month_targets = {d for d in wanted if d.year == year and d.month == month}
        missing = sorted(month_targets - covered)
        if missing and len(missing) < len(month_targets) and len(missing) <= 12:
            for day in missing:
                method, params = _build_request(monthly, str(forest["insttId"]), day, day)
                async with semaphore:
                    await pacer.wait()
                    payload = await _fetch(client, url, method, params)
                rows = extract_rows(payload, monthly.get("rows_path"))
                slots.extend(rows_to_slots(rows, forest, field_map, wanted))

    return slots


class Pacer:
    """요청 시작 시각을 0.5~1초씩 벌려 서버 부담을 줄인다."""

    def __init__(self, min_delay: float, max_delay: float) -> None:
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def wait(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            sleep_for = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + random.uniform(self.min_delay, self.max_delay)
        if sleep_for:
            await asyncio.sleep(sleep_for)


async def _run(
    forests: list[dict[str, Any]],
    endpoints: dict[str, Any],
    targets: list[date],
    cookies: dict[str, str],
    headers: dict[str, str],
) -> QueryReport:
    import httpx

    http_cfg = endpoints.get("http", {})
    semaphore = asyncio.Semaphore(int(http_cfg.get("max_concurrency", 3)))
    pacer = Pacer(
        float(http_cfg.get("min_delay_seconds", 0.5)),
        float(http_cfg.get("max_delay_seconds", 1.0)),
    )
    timeout = float(http_cfg.get("timeout_seconds", 20))

    report = QueryReport()
    all_slots: list[Slot] = []

    async with httpx.AsyncClient(
        cookies=cookies, headers=headers, timeout=timeout, follow_redirects=False
    ) as client:
        tasks = [
            _query_forest(client, forest, endpoints, targets, semaphore, pacer)
            for forest in forests
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    auth_errors = 0
    for forest, result in zip(forests, results):
        if isinstance(result, AuthExpired):
            auth_errors += 1
            report.failed += 1
            report.failures.append(f"{forest['name']}: 세션 만료")
        elif isinstance(result, BaseException):
            report.failed += 1
            report.failures.append(f"{forest['name']}: {type(result).__name__}")
            log.warning("%s 조회 실패: %s", forest["name"], type(result).__name__)
        else:
            report.checked += 1
            all_slots.extend(result)

    # 전부 세션 문제라면 재로그인이 필요하다고 위로 알린다.
    if auth_errors and auth_errors == len(forests):
        raise AuthExpired("모든 휴양림 조회에서 세션이 만료되었습니다")

    report.openings = build_openings(dedup(all_slots))
    log.info(
        "조회 완료: 성공 %d곳 / 실패 %d곳 / 빈자리 %d건",
        report.checked,
        report.failed,
        len(report.openings),
    )
    return report


def query_all(
    forests: list[dict[str, Any]],
    endpoints: dict[str, Any],
    targets: list[date],
    cookies: dict[str, str],
    headers: dict[str, str],
) -> QueryReport:
    """조회를 실행한다.

    Playwright 의 동기 API 는 같은 스레드에서 자기 이벤트 루프를 계속 돌린다.
    그 안에서 asyncio.run() 을 그대로 부르면 "cannot be called from a running
    event loop" 오류가 난다. 그래서 항상 별도 스레드에서 실행한다.
    (스레드 안에서 난 예외는 .result() 가 그대로 다시 던져준다)
    """
    import concurrent.futures

    def worker() -> QueryReport:
        return asyncio.run(_run(forests, endpoints, targets, cookies, headers))

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(worker).result()
