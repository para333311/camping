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
        self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)

    def search(self, arcd: int, start: date, end: date) -> None:
        """검색 폼을 채우고 fn_top_goSearch() 를 그대로 호출한다.

        srchInsttId 는 비워둔다 — 실측 확정 규칙상 비워두면 지역 전체 결과가
        온다(휴양림 하나씩 따로 조회할 필요가 없다). netfunnel_key 는 이
        함수가 내부적으로 채우므로 우리가 만들거나 우회하지 않는다.
        """
        use_dt = f"{cal_format(start)} - {cal_format(end)}"
        js = """(args) => {
            const [arcd, bgDt, edDt, useDt] = args;
            if (window.jQuery) {
                jQuery("#srchInsttArcd").val(arcd);
                jQuery("#srchInsttId").val("");
                jQuery("#rsrvtBgDt").val(bgDt);
                jQuery("#rsrvtEdDt").val(edDt);
                jQuery("#calPicker").val(useDt);
            }
            if (typeof fn_top_goSearch === "function") {
                fn_top_goSearch();
                return true;
            }
            return false;
        }"""
        called = self._page.evaluate(
            js, [str(arcd), start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), use_dt]
        )
        if not called:
            raise RuntimeError("fn_top_goSearch 함수를 찾지 못했습니다 (화면 구조가 바뀐 것으로 보입니다).")

        try:
            self._page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        self._page.wait_for_timeout(1_500)

    def content(self) -> str:
        return self._page.content()
