"""Playwright 로그인 + 검색 실행.

숲나들e 의 객실 조회 화면(fcfsRsrvtPssblGoodsDetls.do 등)은 비로그인으로
호출하면 401 이 오거나 안내 페이지로 리다이렉트된다(실측 확정). 원인은
NetFunnel(대기열) 토큰이 없어서인데, 이 토큰은 페이지의 자바스크립트가
발급하므로 URL 을 직접 조립해 GET 하는 방식은 쓸 수 없다.

그래서 이 모듈은:
  1) Playwright(Chromium, headless) 로 한 번 로그인한다.
  2) 검색 진입 화면으로 이동해, 화면의 jQuery 필드를 채우고
     fn_top_goSearch() 를 그대로 호출한다. netfunnel_key 는 그 함수가
     알아서 채운다 — 우리는 채우지도, 우회하지도 않는다.
  3) 결과가 그려진 페이지의 HTML 을 그대로 돌려준다(파싱은 query.py 가 한다).

브라우저는 실행당 딱 한 번만 띄운다. 엔드포인트를 추측하거나 네트워크를
관찰해 자동학습하는 코드는 여기 없다 — 필요한 주소/파라미터는 모두 실측
확정된 값을 config/endpoints.json 에서 그대로 읽는다.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date
from typing import Any

log = logging.getLogger("foresttrip.session")

WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]


class LoginError(RuntimeError):
    """로그인 실패."""


def cal_format(d: date) -> str:
    """실측 확정된 날짜 표기: 'YY/MM/DD(요일)'. 예: 26/08/15(토)."""
    return f"{d.strftime('%y/%m/%d')}({WEEKDAY_KO[d.weekday()]})"


class ForestSession:
    """로그인된 브라우저 한 개를 감싸고, 검색 실행/결과 페이지 접근을 제공한다."""

    def __init__(self, endpoints: dict[str, Any]) -> None:
        self.endpoints = endpoints
        self.base_url: str = endpoints.get("base_url", "https://www.foresttrip.go.kr").rstrip("/")
        self.user_agent: str = endpoints.get("http", {}).get("user_agent", "Mozilla/5.0")
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

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

    # -- 로그인 ------------------------------------------------------------- #

    def login(self, forest_id: str, forest_pw: str) -> None:
        if not forest_id or not forest_pw:
            raise LoginError("FOREST_ID / FOREST_PW 환경변수가 비어 있습니다.")

        cfg = self.endpoints.get("login", {})
        url = self.base_url + cfg.get("url", "/cmm/lg/loginView.do?hmpgId=FRIP")
        log.info("로그인 페이지로 이동합니다.")
        self._goto_with_retry(url)

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

        if not self._logged_in(cfg):
            raise LoginError(
                "로그인에 실패했습니다. 아이디/비밀번호가 맞는지, "
                "사이트에 추가 인증(캡차 등)이 생기지 않았는지 확인하세요."
            )
        log.info("로그인 성공.")

    def _goto_with_retry(self, url: str) -> None:
        """숲나들e 는 시간대에 따라 응답이 매우 느려진다(실제로 30초 타임아웃이
        간헐적으로 발생했다). 넉넉한 타임아웃으로 몇 번 다시 시도한다.

        재시도는 '접속이 안 될 때' 만 한다 — 로그인 폼 제출을 반복하는 것이
        아니므로 로그인 반복 시도에 해당하지 않는다.
        """
        http_cfg = self.endpoints.get("http", {})
        timeout_ms = int(float(http_cfg.get("nav_timeout_seconds", 60)) * 1000)
        attempts = int(http_cfg.get("nav_retries", 3))

        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                self._page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                return
            except Exception as exc:
                last = exc
                if attempt < attempts:
                    wait_s = 3 * attempt
                    log.warning(
                        "페이지 접속이 느려 %d초 후 다시 시도합니다. (%d/%d, %s)",
                        wait_s,
                        attempt,
                        attempts,
                        type(exc).__name__,
                    )
                    time.sleep(wait_s)

        assert last is not None
        raise last

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

    def cookie_jar(self) -> dict[str, str]:
        jar: dict[str, str] = {}
        try:
            for cookie in self._context.cookies():
                jar[cookie["name"]] = cookie["value"]
        except Exception:
            pass
        return jar

    def page(self) -> Any:
        return self._page

    # -- 검색 (실측 확정 규칙 그대로) ----------------------------------------- #

    def goto_search_entry(self) -> None:
        """실측 확정된 조회 진입점으로 이동한다."""
        cfg = self.endpoints.get("search_entry", {})
        url = self.base_url + cfg.get("url", "/rep/or/fcfsRsrvtMain.do?hmpgId=FRIP&menuId=001001")
        log.info("조회 화면으로 이동합니다.")
        self._goto_with_retry(url)

    def dump_form_controls(self) -> list[dict[str, Any]]:
        """검색 폼의 선택 항목(select/radio/checkbox)과 값을 뽑는다.

        '야영장(데크)만 조회' 를 켜려면 숙박시설/야영장을 구분하는 칸 이름과
        값을 알아야 한다. 그 값을 추측하지 않고 실제 화면에서 읽기 위한 것이다.
        """
        js = """() => {
            const out = [];
            const seen = new Set();

            document.querySelectorAll('select').forEach(s => {
                const key = s.name || s.id;
                if (!key || seen.has('s:' + key)) return;
                seen.add('s:' + key);
                out.push({
                    kind: 'select',
                    name: s.name || '',
                    id: s.id || '',
                    value: s.value || '',
                    options: Array.from(s.options).map(o => ({
                        value: o.value, text: (o.textContent || '').trim()
                    })),
                });
            });

            document.querySelectorAll('input[type=radio], input[type=checkbox]').forEach(i => {
                const key = (i.name || i.id) + '|' + i.value;
                if (seen.has('r:' + key)) return;
                seen.add('r:' + key);
                let label = '';
                if (i.id) {
                    const l = document.querySelector('label[for="' + i.id + '"]');
                    if (l) label = (l.textContent || '').trim();
                }
                if (!label && i.parentElement) label = (i.parentElement.textContent || '').trim();
                out.push({
                    kind: i.type,
                    name: i.name || '',
                    id: i.id || '',
                    value: i.value || '',
                    checked: i.checked,
                    label: label.slice(0, 60),
                });
            });

            // 숙박/야영 구분에 관련돼 보이는 hidden 값도 함께 본다.
            document.querySelectorAll('input[type=hidden]').forEach(i => {
                const n = (i.name || i.id || '');
                if (!/house|camp|sctin|gubun|type|dvsn/i.test(n)) return;
                if (seen.has('h:' + n)) return;
                seen.add('h:' + n);
                out.push({ kind: 'hidden', name: i.name || '', id: i.id || '', value: i.value || '' });
            });

            return out;
        }"""
        try:
            return self._page.evaluate(js) or []
        except Exception as exc:
            log.warning("검색 폼 항목을 읽지 못했습니다: %s", type(exc).__name__)
            return []

    def search(self, arcd: int, start: date, end: date, camp_filter: dict[str, Any] | None = None) -> None:
        """검색 폼을 채우고 fn_top_goSearch() 를 그대로 호출한다.

        srchInsttId 는 비워둔다 — 실측 확정 규칙상 비워두면 지역 전체 결과가
        온다(휴양림 하나씩 따로 조회할 필요가 없다). netfunnel_key 는 이
        함수가 내부적으로 채우므로 우리가 만들거나 우회하지 않는다.

        camp_filter 가 주어지면 그 칸에 그 값을 넣어 야영장(데크)만 걸러낸다.
        값은 실제 화면에서 확인한 것만 쓴다(추측 금지).
        """
        use_dt = f"{cal_format(start)} - {cal_format(end)}"
        filter_field = (camp_filter or {}).get("field") or ""
        filter_value = (camp_filter or {}).get("value")
        js = """(args) => {
            const [arcd, bgDt, edDt, useDt, fField, fValue] = args;
            if (window.jQuery) {
                jQuery("#srchInsttArcd").val(arcd);
                jQuery("#srchInsttId").val("");
                jQuery("#rsrvtBgDt").val(bgDt);
                jQuery("#rsrvtEdDt").val(edDt);
                jQuery("#calPicker").val(useDt);
                if (fField && fValue !== null && fValue !== "") {
                    // id 와 name 둘 다 시도한다(화면마다 다를 수 있다).
                    jQuery("#" + fField).val(fValue);
                    jQuery("[name='" + fField + "']").val(fValue);
                }
            }
            if (typeof fn_top_goSearch === "function") {
                fn_top_goSearch();
                return true;
            }
            return false;
        }"""
        called = self._page.evaluate(
            js,
            [
                str(arcd),
                start.strftime("%Y%m%d"),
                end.strftime("%Y%m%d"),
                use_dt,
                filter_field,
                filter_value if filter_value is not None else "",
            ],
        )
        if not called:
            raise RuntimeError("fn_top_goSearch 함수를 찾지 못했습니다 (화면 구조가 바뀐 것으로 보입니다).")

        # 결과가 그려질 때까지 기다린다. networkidle 을 30초까지 기다리면
        # 날짜 20여 개를 도는 동안 워크플로 제한시간(10분)을 넘길 수 있고,
        # 광고/추적 스크립트가 계속 돌면 끝내 idle 이 안 되기도 한다.
        # 짧게 기다리고, 안 되면 결과 영역이 나타났는지로 판단한다.
        settle_ms = int(float(self.endpoints.get("http", {}).get("settle_seconds", 10)) * 1000)
        try:
            self._page.wait_for_load_state("networkidle", timeout=settle_ms)
        except Exception:
            pass
        self._page.wait_for_timeout(1_500)

    def content(self) -> str:
        return self._page.content()
