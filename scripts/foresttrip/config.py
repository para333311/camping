"""config/*.json 로딩과 저장.

JSON 안에서 밑줄(_)로 시작하는 키는 사람이 읽는 설명이므로 코드는 무시한다.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("foresttrip.config")

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"

DEFAULT_REGION_LABEL = "수도권"


def _read(name: str) -> Any:
    path = CONFIG_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"설정 파일이 없습니다: config/{name}")
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _write(name: str, data: Any) -> None:
    path = CONFIG_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp.replace(path)


def strip_comments(value: Any) -> Any:
    """밑줄로 시작하는 설명용 키를 재귀적으로 제거한다."""
    if isinstance(value, dict):
        return {k: strip_comments(v) for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, list):
        return [strip_comments(v) for v in value]
    return value


# --------------------------------------------------------------------------- #
# regions.json
# --------------------------------------------------------------------------- #

def load_regions() -> tuple[list[int], str]:
    """(srchArea 지역코드 목록, 메시지 제목에 쓸 이름) 을 돌려준다.

    실측 결과 숲나들e 의 지역 필터는 지역명 문자열이 아니라 코드다.
    (1=서울/인천/경기, 2=강원, 3=충북 … foresttrip.forests.REGION_CODE_LABEL 참고)

    regions.json 은 두 가지 형태를 모두 허용한다.
      - [1]                                (단순 배열)
      - {"label": "수도권", "codes": [1]}   (이름까지 지정)
    """
    raw = _read("regions.json")

    if isinstance(raw, list):
        codes = [int(r) for r in raw]
        return codes, DEFAULT_REGION_LABEL

    if isinstance(raw, dict):
        data = strip_comments(raw)
        codes = [int(r) for r in data.get("codes", [])]
        label = str(data.get("label") or DEFAULT_REGION_LABEL).strip() or DEFAULT_REGION_LABEL
        if not codes:
            raise ValueError("config/regions.json 의 codes 가 비어 있습니다.")
        return codes, label

    raise ValueError("config/regions.json 형식을 이해할 수 없습니다.")


# --------------------------------------------------------------------------- #
# schedule.json
# --------------------------------------------------------------------------- #

def load_schedule() -> dict[str, Any]:
    data = strip_comments(_read("schedule.json"))
    start = int(data.get("start_hour", 7))
    end = int(data.get("end_hour", 23))
    if not (0 <= start <= 23 and 0 <= end <= 23):
        raise ValueError("config/schedule.json 의 start_hour / end_hour 는 0~23 사이여야 합니다.")
    if start > end:
        raise ValueError("config/schedule.json 의 start_hour 가 end_hour 보다 큽니다.")
    quiet_hours = [int(h) for h in data.get("quiet_report_hours", [8, 20])]

    return {
        "timezone": data.get("timezone", "Asia/Seoul"),
        "start_hour": start,
        "end_hour": end,
        "heartbeat_hour": int(data.get("heartbeat_hour", start)),
        "quiet_report_hours": quiet_hours,
    }


# --------------------------------------------------------------------------- #
# dates.json
# --------------------------------------------------------------------------- #

def load_dates() -> dict[str, Any]:
    return strip_comments(_read("dates.json"))


# --------------------------------------------------------------------------- #
# endpoints.json
# --------------------------------------------------------------------------- #

def load_endpoints() -> dict[str, Any]:
    return strip_comments(_read("endpoints.json"))


def save_endpoints(updates: dict[str, Any]) -> None:
    """자동학습 결과를 endpoints.json 에 병합 저장한다(설명 키는 보존)."""
    raw = _read("endpoints.json")
    _deep_merge(raw, updates)
    _write("endpoints.json", raw)
    log.info("config/endpoints.json 을 갱신했습니다.")


def _deep_merge(target: dict[str, Any], updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


# --------------------------------------------------------------------------- #
# forests.json (캐시)
# --------------------------------------------------------------------------- #

def load_forest_cache() -> dict[str, Any]:
    try:
        return strip_comments(_read("forests.json"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"collected_at": None, "codes": [], "forests": []}


def save_forest_cache(collected_at: str, codes: list[int], forests: list[dict[str, Any]]) -> None:
    _write(
        "forests.json",
        {
            "_설명": "휴양림 목록 캐시입니다. 봇이 숲나들e 검색 화면에서 자동으로 수집해 채웁니다. 직접 고치지 않아도 됩니다.",
            "_갱신주기": "7일마다 자동으로 다시 수집합니다. 지금 당장 다시 수집하고 싶으면 이 파일을 지우세요.",
            "collected_at": collected_at,
            "codes": codes,
            "forests": forests,
        },
    )
    log.info("휴양림 목록 %d곳을 config/forests.json 에 저장했습니다.", len(forests))


# --------------------------------------------------------------------------- #
# holidays_cache.json (휴양림별 휴장일/최대연박수, 1시간 캐시)
# --------------------------------------------------------------------------- #

def load_holiday_cache() -> dict[str, Any]:
    try:
        return strip_comments(_read("holidays_cache.json"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"collected_at": None, "info": {}}


def save_holiday_cache(collected_at: str, info: dict[str, Any]) -> None:
    _write(
        "holidays_cache.json",
        {
            "_설명": "휴양림별 휴장일/최대연박수 캐시입니다. 봇이 자동으로 채웁니다. 직접 고치지 않아도 됩니다.",
            "_갱신주기": "1시간마다 자동으로 다시 수집합니다.",
            "collected_at": collected_at,
            "info": info,
        },
    )
    log.info("휴양림 %d곳의 휴장일/최대연박수를 config/holidays_cache.json 에 저장했습니다.", len(info))
