"""검색 결과 파싱, 필터링, 연박 묶기.

조회 자체(로그인, 검색 폼 제출)는 session.py 가 Playwright 로 한다. 이
파일은 그렇게 얻은 결과 페이지에서 셀렉터로 행을 뽑아내고(파싱), 필터링과
연박 묶기만 담당한다. HTTP/JSON API 를 직접 호출하는 코드는 여기 없다 —
객실 조회 화면은 로그인 세션 + NetFunnel 토큰이 있어야 열리는 화면이라
JSON 엔드포인트를 따로 부르는 방식 자체가 성립하지 않는다(실측 확정).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable

from .dateplan import group_consecutive

log = logging.getLogger("foresttrip.query")

EXCLUDE_KEYWORD = "예비"  # 운영자 보유분이라 제외
AVAILABLE_MARK = "예약가능"  # 결과 행의 상태 표시에 이 글자가 있어야 빈자리로 본다


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
    # 이제 휴양림 하나씩 조회하지 않고 '날짜 하나당 지역 전체' 를 한 번에
    # 조회하므로, 조회량은 (날짜 수 x 휴양림 수) 로 표시한다.
    dates: int = 0
    forests: int = 0


# --------------------------------------------------------------------------- #
# 결과 페이지 파싱 (셀렉터는 config/endpoints.json 의 result_selectors 에서만 읽는다)
# --------------------------------------------------------------------------- #

def parse_result_rows(session: Any, selectors: dict[str, Any]) -> list[dict[str, Any]]:
    """검색 결과가 그려진 현재 페이지에서 설정된 셀렉터로 행을 뽑는다.

    row 나 forest_name 이 비어 있으면 셀렉터가 아직 설정되지 않은 것이므로
    즉시 실패시킨다(추측해서 채우지 않는다). goods_name/capacity 는 이
    화면에 없을 수 있으므로(휴양림 단위 목록) 선택 항목이다.
    """
    selectors = selectors or {}
    row_sel = selectors.get("row")
    forest_sel = selectors.get("forest_name")
    if not row_sel or not forest_sel:
        raise RuntimeError("셀렉터 미설정")

    goods_sel = selectors.get("goods_name")
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
                availableText: pick(availSel),
            };
        });
    }"""
    rows = session.page().evaluate(js, [row_sel, forest_sel, goods_sel, capacity_sel, available_sel])
    return rows or []


# 휴양림명 앞에 붙는 분류/지역 꼬리표를 떼어낸다.
# 예: "[사립](양평군)양평설매재자연휴양림" -> "양평설매재자연휴양림"
_LABEL_PREFIX = re.compile(r"^(?:\s*[\[(][^\])]*[\])])+")


def _norm_name(name: str) -> str:
    return "".join(str(name or "").split())


def _match_forest(display_name: str, forests_by_len: list[tuple[str, dict[str, Any]]]) -> dict[str, Any] | None:
    """화면에 보이는 이름으로 휴양림을 찾는다.

    화면 이름에는 "[사립](양평군)" 같은 꼬리표가 붙으므로 정확히 일치하지
    않는다. 꼬리표를 떼고 비교하고, 그래도 안 맞으면 '포함' 으로 찾는다.
    짧은 이름이 긴 이름의 일부와 잘못 매칭되지 않도록 긴 이름부터 본다.
    """
    stripped = _norm_name(_LABEL_PREFIX.sub("", display_name))
    whole = _norm_name(display_name)

    for norm, forest in forests_by_len:
        if norm == stripped:
            return forest
    for norm, forest in forests_by_len:
        if norm and (norm in whole):
            return forest
    return None


def rows_to_slots(
    rows: Iterable[dict[str, Any]],
    use_date: date,
    arcd: Any,
    name_to_forest: dict[str, dict[str, Any]],
    available_mark: str = AVAILABLE_MARK,
) -> list[Slot]:
    """파싱된 행을 Slot 으로 바꾼다. 휴양림 이름으로 insttId 를 역매칭한다."""
    forests_by_len = sorted(
        ((_norm_name(k), v) for k, v in name_to_forest.items()),
        key=lambda kv: len(kv[0]),
        reverse=True,
    )
    mark = available_mark or AVAILABLE_MARK

    slots: list[Slot] = []
    for row in rows:
        # 예약 가능 표시(예: "[예약가능]")가 있는 행만 남긴다.
        available_text = str(row.get("availableText") or "")
        if mark not in available_text:
            continue

        goods_name = str(row.get("goods") or "").strip()
        if goods_name and EXCLUDE_KEYWORD in goods_name:
            continue

        display_name = str(row.get("forest") or "").strip()
        forest = _match_forest(display_name, forests_by_len)
        instt_id = str(forest["insttId"]) if forest else ""
        # 화면 이름의 꼬리표를 떼어 보기 좋게 만든다. 목록에서 찾았으면
        # 목록의 이름을 그대로 쓴다.
        clean_name = forest["name"] if forest else _LABEL_PREFIX.sub("", display_name).strip()

        capacity = str(row.get("capacity") or "").strip() or None

        slots.append(
            Slot(
                instt_id=instt_id,
                forest_name=clean_name or "알 수 없음",
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
