"""검색 결과 파싱, 필터링, 연박 묶기.

조회 자체(로그인, 검색 폼 제출)는 session.py 가 Playwright 로 한다. 이
파일은 그렇게 얻은 결과 페이지에서 셀렉터로 행을 뽑아내고(파싱), 필터링과
연박 묶기만 담당한다. HTTP/JSON API 를 직접 호출하는 코드는 여기 없다 —
객실 조회 화면은 로그인 세션 + NetFunnel 토큰이 있어야 열리는 화면이라
JSON 엔드포인트를 따로 부르는 방식 자체가 성립하지 않는다(실측 확정).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable

from .dateplan import group_consecutive

log = logging.getLogger("foresttrip.query")

EXCLUDE_KEYWORD = "예비"  # 운영자 보유분이라 제외


@dataclass
class Slot:
    """객실 하루치 빈자리."""

    instt_id: str
    forest_name: str
    goods_name: str
    use_date: date
    arcd: Any = None  # 딥링크에 쓰는 지역코드(srchInsttArcd)
    capacity: str | None = None

    def key(self) -> tuple[str, date, str]:
        return (self.instt_id or self.forest_name, self.use_date, self.goods_name)


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


@dataclass
class QueryReport:
    openings: list[Opening] = field(default_factory=list)
    checked: int = 0
    failed: int = 0


# --------------------------------------------------------------------------- #
# 결과 페이지 파싱 (셀렉터는 config/endpoints.json 의 result_selectors 에서만 읽는다)
# --------------------------------------------------------------------------- #

def parse_result_rows(session: Any, selectors: dict[str, Any]) -> list[dict[str, Any]]:
    """검색 결과가 그려진 현재 페이지에서 설정된 셀렉터로 행을 뽑는다.

    row/forest_name/goods_name 중 하나라도 비어 있으면 셀렉터가 아직
    설정되지 않은 것이므로 즉시 실패시킨다(추측해서 채우지 않는다).
    """
    selectors = selectors or {}
    row_sel = selectors.get("row")
    forest_sel = selectors.get("forest_name")
    goods_sel = selectors.get("goods_name")
    if not row_sel or not forest_sel or not goods_sel:
        raise RuntimeError("셀렉터 미설정")

    capacity_sel = selectors.get("capacity")
    available_sel = selectors.get("available")

    js = """(args) => {
        const [rowSel, forestSel, goodsSel, capSel, availSel] = args;
        const rows = Array.from(document.querySelectorAll(rowSel));
        return rows.map(r => {
            const pick = (s) => {
                if (!s) return '';
                const el = r.querySelector(s);
                return el ? el.innerText.trim() : '';
            };
            return {
                forest: pick(forestSel),
                goods: pick(goodsSel),
                capacity: pick(capSel),
                available: availSel ? !!r.querySelector(availSel) : true,
            };
        });
    }"""
    rows = session.page().evaluate(js, [row_sel, forest_sel, goods_sel, capacity_sel, available_sel])
    return rows or []


def _norm_name(name: str) -> str:
    return "".join(str(name or "").split())


def rows_to_slots(
    rows: Iterable[dict[str, Any]],
    use_date: date,
    arcd: Any,
    name_to_forest: dict[str, dict[str, Any]],
) -> list[Slot]:
    """파싱된 행을 Slot 으로 바꾼다. 휴양림 이름으로 insttId 를 역매칭한다."""
    lookup = {_norm_name(k): v for k, v in name_to_forest.items()}

    slots: list[Slot] = []
    for row in rows:
        goods_name = str(row.get("goods") or "").strip()
        if not goods_name or EXCLUDE_KEYWORD in goods_name:
            continue
        if not row.get("available", True):
            continue

        forest_name = str(row.get("forest") or "").strip()
        forest = lookup.get(_norm_name(forest_name))
        instt_id = str(forest["insttId"]) if forest else ""

        capacity = str(row.get("capacity") or "").strip() or None

        slots.append(
            Slot(
                instt_id=instt_id,
                forest_name=forest_name or (forest["name"] if forest else "알 수 없음"),
                goods_name=goods_name,
                use_date=use_date,
                arcd=arcd,
                capacity=capacity,
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


def build_openings(slots: Iterable[Slot], max_nights: dict[str, int] | None = None) -> list[Opening]:
    """같은 (휴양림, 객실)의 빈 날짜를 연속 구간으로 묶는다.

    max_nights 에 해당 휴양림의 최대 연박수(mxmmStngDayCnt)가 있으면 그
    길이만큼씩 잘라서 여러 건으로 알린다.
    """
    from collections import defaultdict

    max_nights = max_nights or {}
    buckets: dict[tuple[str, str], list[Slot]] = defaultdict(list)
    for slot in slots:
        buckets[(slot.instt_id or slot.forest_name, slot.goods_name)].append(slot)

    openings: list[Opening] = []
    for (key, goods_name), group in buckets.items():
        by_date = {s.use_date: s for s in group}
        cap = max_nights.get(key)
        for run in group_consecutive(by_date.keys()):
            chunk_size = cap if cap and cap > 0 else len(run)
            for start_idx in range(0, len(run), chunk_size):
                chunk = run[start_idx : start_idx + chunk_size]
                first = by_date[chunk[0]]
                openings.append(
                    Opening(
                        instt_id=first.instt_id,
                        forest_name=first.forest_name,
                        goods_name=goods_name,
                        start=chunk[0],
                        end=chunk[-1] + timedelta(days=1),
                        nights=len(chunk),
                        arcd=first.arcd,
                        capacity=first.capacity,
                    )
                )

    # 정렬: 박수 많은 순 -> 날짜 빠른 순 -> 휴양림명
    openings.sort(key=lambda o: (-o.nights, o.start, o.forest_name, o.goods_name))
    return openings
