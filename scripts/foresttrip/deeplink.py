"""예약 화면 deep link 생성.

알림에서 객실을 누르면 그 휴양림의 그 날짜 예약 화면으로 바로 들어가야 한다.
사용자 브라우저에 숲나들e 로그인 세션이 있으면 로그인 화면을 거치지 않는다
(링크 자체에는 어떤 인증정보도 담기지 않는다).

우선순위
  1) discovered : 자동학습이 실제 화면에서 관찰한 예약 링크 규칙
  2) primary    : insttId + 체크인/체크아웃 날짜를 붙인 시설 예약 화면
  3) fallback   : 날짜 없이 휴양림 화면까지만
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

log = logging.getLogger("foresttrip.deeplink")

_warned = False


def build(endpoints: dict[str, Any], instt_id: str, start: date, end: date,
          goods_id: str | None = None) -> str:
    global _warned

    cfg = endpoints.get("deeplink", {})
    base = endpoints.get("base_url", "https://www.foresttrip.go.kr").rstrip("/")
    fmt = cfg.get("date_format", "%Y%m%d")

    values = {
        "base": base,
        "instt": instt_id,
        "date_from": start.strftime(fmt),
        "date_to": end.strftime(fmt),
        "goods": goods_id or "",
    }

    for key in ("discovered", "primary", "fallback"):
        template = cfg.get(key)
        if not template:
            continue
        try:
            url = str(template).format(**values)
        except KeyError as exc:
            log.warning("deeplink.%s 템플릿에 모르는 항목이 있습니다: %s", key, exc)
            continue
        if key == "fallback" and not _warned:
            _warned = True
            log.warning("날짜가 붙은 예약 링크를 만들지 못해 휴양림 화면 링크로 대체합니다.")
        return url

    return f"{base}/pot/is/fs/selectFcltSrchView.do?hmpgId=FRIP&menuId=002001&insttId={instt_id}"
