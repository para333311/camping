"""예약 화면 deep link 생성 (실측 확정, 추측 아님).

숲나들e 실제 페이지 소스에서 확인한 규칙을 그대로 쓴다. 휴양림 카드의
  fn_goRsvrtTheme('1','ID02030023','동두천자연휴양림 ','fcfsRsrvt','theme_ID02030023')
호출이 아래 폼 필드를 채운 뒤 GET 으로 제출한다.
  arcd(지역코드)   -> srchInsttArcd
  insttId          -> srchInsttId
  휴양림명         -> srchWord
flag 가 'fcfsRsrvt' 일 때 제출 대상은 /rep/or/sssn/fcfsRsrvtPssblGoodsDetls.do 다.

비로그인으로 열면 401 이 뜨는 것이 정상이다 — 사용자 브라우저에 숲나들e
로그인 세션이 있으면 그대로 예약 화면이 열린다. NetFunnel(대기열)은 그대로
두고 우회하지 않는다.

체크인/체크아웃 날짜(cal_format 반환값)는 실측하지 못해 이 링크에는
포함하지 않는다. 날짜 없이 열어도 예약 가능 객실 화면 자체는 뜬다.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote


def build(endpoints: dict[str, Any], instt_id: str, forest_name: str, arcd: Any) -> str:
    cfg = endpoints.get("deeplink", {})
    base = endpoints.get("base_url", "https://www.foresttrip.go.kr").rstrip("/")
    template = cfg.get(
        "template",
        "{base}/rep/or/sssn/fcfsRsrvtPssblGoodsDetls.do"
        "?srchInsttArcd={arcd}&srchInsttId={instt}&srchWord={name}",
    )
    return template.format(
        base=base,
        arcd=arcd,
        instt=instt_id,
        name=quote(str(forest_name or ""), safe=""),
    )
