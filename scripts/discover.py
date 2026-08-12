#!/usr/bin/env python3
"""엔드포인트 자동학습만 따로 실행해보는 도구.

봇이 사이트 주소를 못 알아낼 때, 이것만 따로 돌려서 무엇을 찾았는지 확인한다.

    python scripts/discover.py

찾아낸 내용은 config/endpoints.json 에 저장된다.
비밀번호는 화면에 절대 출력되지 않는다.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from foresttrip import config, dateplan, forests as forests_mod
from foresttrip.logging_setup import setup_logging
from foresttrip.session import ForestSession, analyse_payload

log = setup_logging(verbose=True)


def main() -> int:
    endpoints = config.load_endpoints()
    regions, _label = config.load_regions()
    targets = dateplan.build_target_dates(config.load_dates(), __import__("datetime").date.today())
    if not targets:
        log.error("조회할 날짜가 없습니다. config/dates.json 을 확인하세요.")
        return 1

    with ForestSession(endpoints) as session:
        session.login(os.environ.get("FOREST_ID", ""), os.environ.get("FOREST_PW", ""))
        forest_list = forests_mod.get_forests(session, endpoints, regions)
        log.info("휴양림 %d곳: %s", len(forest_list),
                 ", ".join(f["name"] for f in forest_list[:10]))

        sample = forest_list[0]
        spec = session.discover_monthly(str(sample["insttId"]), targets[0])

        if not spec:
            log.error("월별예약조회 요청을 찾지 못했습니다.")
            log.info("대신 관찰된 JSON 응답 %d개를 아래에 요약합니다.", len(session.captured))
            for entry in session.captured[:20]:
                path, rows, score = analyse_payload(entry["payload"])
                log.info(
                    "  %s %s -> 점수 %d, 목록위치 %r, 행 %d개",
                    entry["method"],
                    entry["url"].split("?")[0],
                    score,
                    path,
                    len(rows),
                )
            return 1

        sample_row = spec.pop("_sample_row", None)
        config.save_endpoints(spec)

        print("\n===== 찾아낸 엔드포인트 =====")
        print(json.dumps(spec, ensure_ascii=False, indent=2))
        if sample_row:
            print("\n===== 응답 한 줄 예시 =====")
            print(json.dumps(sample_row, ensure_ascii=False, indent=2))
        print("\nconfig/endpoints.json 에 저장했습니다.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
