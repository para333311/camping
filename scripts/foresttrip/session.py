"""Playwright 로그인 + 엔드포인트 자동학습.

숲나들e 는 비로그인 상태에서 월별예약조회 화면이 401 을 돌려주고, 조회
endpoint 도 JSON 대신 안내 HTML 을 준다. 그래서 로그인이 필수다.

흐름:
  1) Playwright(Chromium, headless) 로 한 번 로그인한다.
  2) 로그인한 브라우저로 실제 예약 화면을 한 번 열어보면서, 그 화면이
     어떤 주소로 어떤 JSON 을 받아오는지 네트워크를 그대로 관찰한다.
     (= 엔드포인트를 추측하지 않고 사이트에서 직접 알아낸다)
  3) 알아낸 결과를 config/endpoints.json 에 저장한다.
  4) 쿠키를 꺼내 httpx 로 넘긴다. 이후 조회는 가벼운 HTTP 요청만 쓴다.

브라우저는 실행당 딱 한 번만 띄운다.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Any

log = logging.getLogger("foresttrip.session")

_DATE_VALUE = re.compile(r"^\s*(\d{4})[-./]?(\d{2})[-./]?(\d{2})\s*$")
_CSRF_HEADER_HINT = re.compile(r"(?i)csrf|xsrf")


class LoginError(RuntimeError):
    """로그인 실패."""


# --------------------------------------------------------------------------- #
# JSON 응답 분석 (관찰 기반 학습)
# --------------------------------------------------------------------------- #

def _looks_like_date(value: Any) -> bool:
    if isinstance(value, (int, float)):
        value = str(int(value))
    if not isinstance(value, str):
        return False
    match = _DATE_VALUE.match(value)
    if not match:
        return False
    year, month, day = (int(g) for g in match.groups())
    return 2000 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31


def _score_rows(rows: list[Any]) -> int:
    """이 배열이 '객실 하루치 목록'처럼 보이는 정도를 점수로 매긴다."""
    dict_rows = [r for r in rows if isinstance(r, dict)]
    if not dict_rows:
        return 0

    sample = dict_rows[:40]
    keys: set[str] = set()
    for row in sample:
        keys.update(row.keys())

    score = 0
    lowered = {k.lower() for k in keys}

    # 날짜처럼 생긴 값이 실제로 들어있는가 (가장 강한 신호)
    if any(_looks_like_date(v) for row in sample for v in row.values()):
        score += 40
    # 사용자가 알려준 확정 필드명
    if "usedt" in lowered:
        score += 30
    if "goodsnm" in lowered:
        score += 30
    if "insttid" in lowered:
        score += 15
    # 이름/코드류 필드가 섞여 있는가
    if any(k.endswith("nm") for k in lowered):
        score += 10
    if any("cnt" in k or k.endswith("yn") for k in lowered):
        score += 10
    # 하루치 행이 여러 개면 목록일 가능성이 높다
    score += min(len(dict_rows), 20)
    return score


def _walk_json(node: Any, path: str = "") -> list[tuple[str, list[Any]]]:
    """JSON 안의 모든 '딕셔너리 배열' 을 (경로, 배열) 로 모아준다."""
    found: list[tuple[str, list[Any]]] = []
    if isinstance(node, list):
        if any(isinstance(item, dict) for item in node):
            found.append((path, node))
        for item in node[:5]:
            found.extend(_walk_json(item, path))
    elif isinstance(node, dict):
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else key
            found.extend(_walk_json(value, child_path))
    return found


def analyse_payload(payload: Any) -> tuple[str | None, list[dict[str, Any]], int]:
    """응답에서 가장 객실 목록다운 배열과 그 경로, 점수를 고른다."""
    best_path: str | None = None
    best_rows: list[dict[str, Any]] = []
    best_score = 0
    for path, rows in _walk_json(payload):
        score = _score_rows(rows)
        if score > best_score:
            best_score = score
            best_path = path
            best_rows = [r for r in rows if isinstance(r, dict)]
    return best_path, best_rows, best_score


def infer_field_map(rows: list[dict[str, Any]], current: dict[str, Any]) -> dict[str, Any]:
    """관찰한 행에서 각 정보가 어느 키에 들어있는지 추론한다."""
    if not rows:
        return {}

    keys: list[str] = []
    for row in rows[:40]:
        for key in row.keys():
            if key not in keys:
                keys.append(key)

    lower = {k.lower(): k for k in keys}
    inferred: dict[str, Any] = {}

    def pick(target: str, exact: str | None, candidates: list[str]) -> None:
        # 1순위: 기존 설정값이 실제로 존재하면 그대로 둔다.
        if exact and exact in keys:
            inferred[target] = exact
            return
        # 2순위: 후보 이름 중 실제로 존재하는 것
        for cand in candidates:
            if cand.lower() in lower:
                inferred[target] = lower[cand.lower()]
                return

    pick("use_date", current.get("use_date"), ["useDt", "useDate", "rsvtDt", "dt"])
    pick("goods_name", current.get("goods_name"), ["goodsNm", "roomNm", "prdtNm"])
    pick("instt_id", current.get("instt_id"), ["insttId", "instId"])
    pick("capacity", current.get("capacity"), current.get("capacity_candidates", []))
    pick("remain", current.get("remain"), current.get("remain_candidates", []))
    pick("available_flag", current.get("available_flag"), current.get("available_flag_candidates", []))
    pick("goods_id", current.get("goods_id"), current.get("goods_id_candidates", []))

    # 날짜 필드를 못 찾았으면 값 모양으로 찾는다.
    if "use_date" not in inferred:
        for key in keys:
            if all(_looks_like_date(row.get(key)) for row in rows[:10] if key in row):
                inferred["use_date"] = key
                break

    # 인원 필드를 못 찾았으면 '작은 정수' 이면서 이름에 인원 힌트가 있는 키를 찾는다.
    if "capacity" not in inferred:
        for key in keys:
            if not re.search(r"(?i)nmpr|psn|capa|인원|person", key):
                continue
            values = [row.get(key) for row in rows[:10] if row.get(key) is not None]
            if values and all(str(v).strip().isdigit() and 1 <= int(str(v)) <= 30 for v in values):
                inferred["capacity"] = key
                break

    inferred["_추론시각"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    inferred["_관찰된_키목록"] = keys[:60]
    return inferred


# --------------------------------------------------------------------------- #
# 세션
# --------------------------------------------------------------------------- #

class ForestSession:
    """로그인된 브라우저 한 개를 감싸고, 쿠키/엔드포인트를 뽑아준다."""

    def __init__(self, endpoints: dict[str, Any]) -> None:
        self.endpoints = endpoints
        self.base_url: str = endpoints.get("base_url", "https://www.foresttrip.go.kr").rstrip("/")
        self.user_agent: str = endpoints.get("http", {}).get("user_agent", "Mozilla/5.0")
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self.csrf: dict[str, str] = {}
        self.captured: list[dict[str, Any]] = []

    # -- 생명주기 ----------------------------------------------------------- #

    def __enter__(self) -> "ForestSession":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        launch_args: dict[str, Any] = {
            "headless": True,
            "args": ["--no-sandbox", "--disable-dev-shm-usage"],
        }
        # 크롬이 다른 곳에 설치된 환경에서도 쓸 수 있게 하는 탈출구.
        # GitHub Actions 에서는 설정하지 않아도 됩니다.
        custom_chrome = os.environ.get("PLAYWRIGHT_EXECUTABLE_PATH")
        if custom_chrome:
            launch_args["executable_path"] = custom_chrome
        self._browser = self._pw.chromium.launch(**launch_args)
        self._context = self._browser.new_context(
            user_agent=self.user_agent,
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            viewport={"width": 1440, "height": 900},
        )
        self._context.set_default_timeout(30_000)
        self._page = self._context.new_page()
        self._install_recorder()
        return self

    def __exit__(self, *exc: Any) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    # -- 네트워크 관찰 ------------------------------------------------------- #

    def _install_recorder(self) -> None:
        """오가는 JSON 응답을 전부 기록해 둔다(엔드포인트 학습용)."""

        def on_response(response: Any) -> None:
            try:
                url = response.url
                if self.base_url not in url:
                    return
                ctype = (response.headers or {}).get("content-type", "")
                if "json" not in ctype.lower():
                    return
                body = response.json()
            except Exception:
                return

            request = response.request
            post_data: Any = None
            try:
                post_data = request.post_data
            except Exception:
                post_data = None

            self.captured.append(
                {
                    "url": url,
                    "method": request.method,
                    "post_data": post_data,
                    "payload": body,
                }
            )

        self._page.on("response", on_response)

        def on_request(request: Any) -> None:
            try:
                for name, value in (request.headers or {}).items():
                    if _CSRF_HEADER_HINT.search(name) and value:
                        self.csrf[name] = value
            except Exception:
                pass

        self._page.on("request", on_request)

    # -- 로그인 ------------------------------------------------------------- #

    def login(self, forest_id: str, forest_pw: str) -> None:
        if not forest_id or not forest_pw:
            raise LoginError("FOREST_ID / FOREST_PW 환경변수가 비어 있습니다.")

        cfg = self.endpoints.get("login", {})
        url = self.base_url + cfg.get("url", "/cmm/lg/loginView.do?hmpgId=FRIP")
        log.info("로그인 페이지로 이동합니다.")
        self._page.goto(url, wait_until="domcontentloaded")

        id_box = self._first_visible(cfg.get("id_selectors", []))
        pw_box = self._first_visible(cfg.get("pw_selectors", []))
        if not id_box or not pw_box:
            raise LoginError(
                "로그인 입력칸을 찾지 못했습니다. "
                "config/endpoints.json 의 login.id_selectors / pw_selectors 를 확인하세요."
            )

        id_box.fill(forest_id)
        pw_box.fill(forest_pw)

        submit = self._first_visible(cfg.get("submit_selectors", []))
        try:
            with self._page.expect_navigation(wait_until="domcontentloaded", timeout=30_000):
                if submit:
                    submit.click()
                else:
                    pw_box.press("Enter")
        except Exception:
            # SPA 형태라 navigation 이 안 잡히는 경우가 있어 무시하고 검증으로 넘어간다.
            self._page.wait_for_timeout(2_000)

        self._capture_csrf_from_page()

        if not self._logged_in(cfg):
            raise LoginError(
                "로그인에 실패했습니다. 아이디/비밀번호가 맞는지, "
                "사이트에 추가 인증(캡차 등)이 생기지 않았는지 확인하세요."
            )
        log.info("로그인 성공.")

    def _first_visible(self, selectors: list[str]) -> Any:
        for selector in selectors:
            try:
                locator = self._page.locator(selector).first
                if locator.count() > 0 and locator.is_visible():
                    return locator
            except Exception:
                continue
        return None

    def _logged_in(self, cfg: dict[str, Any]) -> bool:
        texts = (cfg.get("success_check") or {}).get("any_text", ["로그아웃"])
        try:
            content = self._page.content()
        except Exception:
            return False
        return any(t in content for t in texts)

    def _capture_csrf_from_page(self) -> None:
        try:
            found = self._page.evaluate(
                """() => {
                    const out = {};
                    document.querySelectorAll('meta[name]').forEach(m => {
                        if (/csrf|xsrf/i.test(m.getAttribute('name') || '')) {
                            out[m.getAttribute('name')] = m.getAttribute('content') || '';
                        }
                    });
                    document.querySelectorAll('input[type=hidden][name]').forEach(i => {
                        if (/csrf|xsrf|token/i.test(i.getAttribute('name') || '')) {
                            out[i.getAttribute('name')] = i.value || '';
                        }
                    });
                    return out;
                }"""
            )
            for key, value in (found or {}).items():
                if value:
                    self.csrf[key] = value
            if self.csrf:
                log.info("CSRF 토큰 %d개를 확보했습니다.", len(self.csrf))
        except Exception:
            pass

    # -- httpx 로 넘길 재료 -------------------------------------------------- #

    def cookie_jar(self) -> dict[str, str]:
        jar: dict[str, str] = {}
        try:
            for cookie in self._context.cookies():
                jar[cookie["name"]] = cookie["value"]
        except Exception:
            pass
        return jar

    def request_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "ko-KR,ko;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.base_url}/pot/is/fs/selectFcltSrchView.do?hmpgId=FRIP&menuId=002001",
        }
        # 관찰한 CSRF 헤더를 그대로 실어보낸다.
        for name, value in self.csrf.items():
            if _CSRF_HEADER_HINT.search(name):
                headers[name if name.lower().startswith("x-") else f"X-{name}"] = value
        return headers

    def page(self) -> Any:
        return self._page

    # -- 엔드포인트 자동학습 -------------------------------------------------- #

    def discover_monthly(self, instt_id: str, target: date) -> dict[str, Any] | None:
        """실제 예약 화면을 열어보며 월별예약조회 요청을 관찰한다."""
        from datetime import timedelta

        ym = f"{target.year:04d}{target.month:02d}"
        ymd = target.strftime("%Y%m%d")
        # 시작일과 종료일을 일부러 다르게 줘야 어느 칸이 시작이고 어느 칸이 끝인지
        # 구분할 수 있다.
        ymd_end = (target + timedelta(days=1)).strftime("%Y%m%d")
        self.captured.clear()

        detail = (
            f"{self.base_url}/pot/is/fs/selectFcltSrchView.do"
            f"?hmpgId=FRIP&menuId=002001&insttId={instt_id}"
            f"&useBgnDtm={ymd}&useEndDtm={ymd_end}"
        )
        log.info("엔드포인트 학습을 위해 예약 화면을 열어봅니다. (insttId=%s)", instt_id)
        try:
            self._page.goto(detail, wait_until="networkidle", timeout=45_000)
        except Exception:
            log.warning("예약 화면 로딩이 느려 시간 초과했습니다. 관찰된 요청만으로 진행합니다.")

        self._nudge_monthly_view()
        self._page.wait_for_timeout(2_500)
        self._capture_csrf_from_page()

        best = self._best_capture()
        if not best:
            log.warning("월별예약조회 JSON 요청을 관찰하지 못했습니다.")
            return None

        return self._capture_to_spec(best, instt_id, ym, ymd, ymd_end)

    def _nudge_monthly_view(self) -> None:
        """월별예약조회 탭/버튼이 있으면 눌러서 조회 요청을 유발한다."""
        candidates = [
            "a:has-text('월별예약조회')",
            "button:has-text('월별예약조회')",
            "a:has-text('월별')",
            "a:has-text('예약조회')",
            "a:has-text('객실예약')",
            "a:has-text('실시간예약')",
            "button:has-text('조회')",
        ]
        for selector in candidates:
            try:
                locator = self._page.locator(selector).first
                if locator.count() > 0 and locator.is_visible():
                    locator.click(timeout=5_000)
                    self._page.wait_for_timeout(2_000)
                    if self._best_capture():
                        return
            except Exception:
                continue

    def _best_capture(self) -> dict[str, Any] | None:
        best = None
        best_score = 0
        for entry in self.captured:
            _, rows, score = analyse_payload(entry["payload"])
            # 최소 점수 미만이면 우리가 찾는 목록이 아니다.
            if score >= 50 and score > best_score and rows:
                best_score = score
                best = entry
        return best

    def _capture_to_spec(
        self, entry: dict[str, Any], instt_id: str, ym: str, ymd: str, ymd_end: str
    ) -> dict[str, Any]:
        """관찰한 요청 하나를 재현 가능한 명세로 바꾼다."""
        from urllib.parse import parse_qsl, urlsplit

        split = urlsplit(entry["url"])
        params: dict[str, str] = dict(parse_qsl(split.query))

        body_params: dict[str, str] = {}
        post_data = entry.get("post_data")
        if post_data:
            try:
                body_params = dict(json.loads(post_data))
            except Exception:
                try:
                    body_params = dict(parse_qsl(post_data))
                except Exception:
                    body_params = {}

        merged = {**params, **body_params}

        # 값으로 역추적해서 파라미터 '이름' 을 알아낸다.
        def _date_forms(value: str) -> tuple[str, ...]:
            return (value, f"{value[:4]}-{value[4:6]}-{value[6:]}", f"{value[:4]}.{value[4:6]}.{value[6:]}")

        param_names: dict[str, str] = {}
        for name, value in merged.items():
            text = str(value)
            if text == instt_id:
                param_names["instt"] = name
            elif text == ym:
                param_names["ym"] = name
            elif text in _date_forms(ymd):
                param_names.setdefault("date_from", name)
            elif text in _date_forms(ymd_end):
                param_names.setdefault("date_to", name)

        path, rows, _score = analyse_payload(entry["payload"])
        field_map = infer_field_map(rows, self.endpoints.get("field_map", {}))

        if "instt" not in param_names:
            log.warning(
                "요청에서 휴양림 번호가 들어가는 칸 이름을 찾지 못해 기본값(insttId)을 씁니다. "
                "휴양림별 결과가 이상하면 README 의 '엔드포인트 직접 알아내는 법' 을 보세요."
            )

        log.info(
            "월별예약조회 엔드포인트를 찾았습니다: %s %s (행 %d개, 목록 위치 %r)",
            entry["method"],
            split.path,
            len(rows),
            path,
        )

        return {
            "monthly": {
                "url": split.path,
                "method": entry["method"],
                "params": {k: v for k, v in merged.items()},
                "param_names": {**self.endpoints.get("monthly", {}).get("param_names", {}), **param_names},
                "rows_path": path,
            },
            "field_map": field_map,
            "discovered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "discovery_note": f"자동학습 성공: {entry['method']} {split.path}",
            "_sample_row": rows[0] if rows else None,
        }

