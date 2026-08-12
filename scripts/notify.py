#!/usr/bin/env python3
"""숲나들e 수도권 휴양림 빈자리 텔레그램 알림봇.

하는 일
  1) 지금이 알림 시간대(KST)인지 확인한다. 아니면 아무것도 하지 않고 끝낸다.
  2) 휴양림 목록(JSON API, 로그인 불필요)과 휴양림별 휴장일/최대연박수
     (JSON API, 로그인 불필요, 1시간 캐시)를 가져온다.
  3) Playwright 로 로그인 1회 → 검색 화면에서 날짜별로 검색을 실행한다
     (실측 확정된 jQuery 필드 채우기 + fn_top_goSearch() 호출).
  4) 결과 페이지를 설정된 셀렉터로 파싱하고, 휴장일 제외 + 최대연박수 캡을
     적용한 뒤 연박으로 묶는다.
  5) 직전 결과와 완전히 같으면 재전송하지 않는다(actions/cache 로 비교).
     빈자리가 있으면 즉시 소리 알림, 없으면 KST 08시/20시만 무음 발송.

절대 하지 않는 일
  예약 신청, 결제, 캡차/대기열 우회, 로그인 반복 시도.
  NetFunnel(대기열) 토큰은 사이트 자바스크립트가 채우게 두고 우리가
  만들거나 우회하지 않는다.

사용법
  python scripts/notify.py             # 정상 실행 (시간대 검사 O, 발송 O)
  python scripts/notify.py --dry-run   # 시간대 검사 X, 발송 X, 결과만 화면에 출력
  python scripts/notify.py --force     # 시간대 검사 X, 발송 O (수동 실행용)

환경변수
  DUMP_HTML=1  이면 검색 결과 화면 HTML 발췌를 텔레그램으로 보내고 끝낸다.
               (result_selectors 를 알아내기 위한 진단 모드)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from foresttrip import config, dateplan, forests as forests_mod, holidays as holidays_mod
from foresttrip import htmldump, message, query, telegram
from foresttrip.logging_setup import setup_logging
from foresttrip.session import ForestSession

log = setup_logging()

REPO_ROOT = Path(__file__).resolve().parent.parent
LAST_SEEN_PATH = REPO_ROOT / ".last_seen.json"


# --------------------------------------------------------------------------- #
# 단계 태그가 붙은 실패
# --------------------------------------------------------------------------- #

class StageFailure(Exception):
    """어느 단계에서 실패했는지 들고 다니는 예외.

    텔레그램/로그에 "[단계] 휴양림목록 / RuntimeError / ..." 형태로 그대로
    노출하기 위한 것이다. 원인을 뭉뚱그린 한국어 문구로 바꾸지 않는다.
    """

    def __init__(self, stage: str, original: BaseException) -> None:
        super().__init__(str(original))
        self.stage = stage
        self.original = original


def _stage(name: str, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except StageFailure:
        raise
    except Exception as exc:
        raise StageFailure(name, exc) from exc


# --------------------------------------------------------------------------- #
# 시간
# --------------------------------------------------------------------------- #

def now_in(tz_name: str) -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        log.warning("시간대 %s 를 못 찾아 UTC+9 로 대체합니다.", tz_name)
        return datetime.now(timezone(timedelta(hours=9)))


def within_window(now: datetime, schedule: dict[str, Any]) -> bool:
    return schedule["start_hour"] <= now.hour <= schedule["end_hour"]


# --------------------------------------------------------------------------- #
# 직전 결과와 비교(중복 억제) — actions/cache 로 유지되는 파일, 커밋하지 않는다
# --------------------------------------------------------------------------- #

def _digest(openings: list[query.Opening]) -> str:
    parts = sorted(
        f"{o.instt_id}|{o.forest_name}|{o.goods_name}|{o.start.isoformat()}|{o.end.isoformat()}"
        for o in openings
    )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _load_last_digest() -> str | None:
    try:
        data = json.loads(LAST_SEEN_PATH.read_text(encoding="utf-8"))
        return data.get("digest")
    except Exception:
        return None


def _save_last_digest(digest: str) -> None:
    try:
        LAST_SEEN_PATH.write_text(json.dumps({"digest": digest}), encoding="utf-8")
    except Exception as exc:
        log.warning("직전 결과 캐시 파일을 쓰지 못했습니다: %s", type(exc).__name__)


# --------------------------------------------------------------------------- #
# 본체
# --------------------------------------------------------------------------- #

@dataclass
class RunResult:
    report: query.QueryReport
    label: str
    heartbeat: bool
    endpoints: dict[str, Any]


def _run_dump_html(session: ForestSession, endpoints: dict[str, Any], code: int, sample_date) -> None:
    """DUMP_HTML=1 모드: 검색 결과 HTML 발췌를 텔레그램으로 보내고 끝낸다."""
    session.goto_search_entry()
    session.search(code, sample_date, sample_date + timedelta(days=1))
    html = session.content()

    snippet = htmldump.extract_snippet(html)
    messages = htmldump.split_for_telegram(snippet)

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    for i, chunk in enumerate(messages):
        telegram.send(token, chat_id, chunk, silent=True)
        if i < len(messages) - 1:
            time.sleep(0.5)
    log.info("DUMP_HTML 모드: HTML 발췌 %d개 메시지를 보냈습니다.", len(messages))


def run(args: argparse.Namespace) -> RunResult | None:
    schedule = config.load_schedule()
    codes, label = config.load_regions()
    date_cfg = config.load_dates()
    endpoints = config.load_endpoints()

    now = now_in(schedule["timezone"])
    targets = dateplan.build_target_dates(date_cfg, now.date())
    if not targets:
        log.warning("조회할 날짜가 없습니다. config/dates.json 을 확인하세요.")

    forest_list = _stage("휴양림목록", forests_mod.get_forests, endpoints, codes)
    holiday_info = _stage("가능일조회", holidays_mod.get_all, endpoints, forest_list)

    forest_id = os.environ.get("FOREST_ID", "")
    forest_pw = os.environ.get("FOREST_PW", "")
    dump_html = os.environ.get("DUMP_HTML", "").strip().lower() in ("1", "true", "yes")
    primary_code = codes[0]

    # 브라우저를 띄우는 것(Playwright __enter__)도 로그인 단계의 일부로 본다 —
    # 사용자에게는 "로그인이 안 됐다" 로 보이는 실패이기 때문이다.
    session = ForestSession(endpoints)
    _stage("로그인", session.__enter__)
    try:
        _stage("로그인", session.login, forest_id, forest_pw)

        if dump_html:
            sample_date = targets[0] if targets else (now.date() + timedelta(days=3))
            _stage("조회", _run_dump_html, session, endpoints, primary_code, sample_date)
            return None

        def _do_query() -> dict[Any, list[dict[str, Any]]]:
            session.goto_search_entry()
            selectors = endpoints.get("result_selectors") or {}
            raw: dict[Any, list[dict[str, Any]]] = {}
            for i, target_date in enumerate(targets):
                if i:
                    time.sleep(random.uniform(0.5, 1.0))
                session.search(primary_code, target_date, target_date + timedelta(days=1))
                raw[target_date] = query.parse_result_rows(session, selectors)
            return raw

        raw_by_date = _stage("조회", _do_query)
    finally:
        session.__exit__(None, None, None)

    # 브라우저를 벗어난 뒤 순수 파이썬으로 필터링/그룹핑한다.
    def _post_process() -> list[query.Opening]:
        name_to_forest = {f["name"]: f for f in forest_list}
        all_slots: list[query.Slot] = []
        for target_date, rows in raw_by_date.items():
            slots = query.rows_to_slots(rows, target_date, primary_code, name_to_forest)
            for slot in slots:
                info = holiday_info.get(slot.instt_id)
                if not info:
                    all_slots.append(slot)
                    continue
                iso = target_date.isoformat()
                if iso in (info.get("closed") or []):
                    continue  # (b) 휴장일
                if info.get("open") and iso not in info["open"]:
                    continue  # (a) 예약 오픈 범위 밖
                all_slots.append(slot)

        max_nights = {
            iid: info["maxNights"]
            for iid, info in holiday_info.items()
            if info.get("maxNights")
        }
        return query.build_openings(query.dedup(all_slots), max_nights)

    openings = _stage("파싱", _post_process)

    report = query.QueryReport(openings=openings, checked=len(targets), failed=0)
    heartbeat = now.hour == schedule["heartbeat_hour"]
    return RunResult(report=report, label=label, heartbeat=heartbeat, endpoints=endpoints)


def main() -> int:
    parser = argparse.ArgumentParser(description="숲나들e 휴양림 빈자리 알림봇")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="텔레그램으로 보내지 않고 결과만 화면에 출력합니다. 시간대 검사도 건너뜁니다.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="시간대 검사를 건너뛰고 무조건 실행합니다. (수동 실행용)",
    )
    parser.add_argument("--verbose", action="store_true", help="자세한 로그를 봅니다.")
    args = parser.parse_args()

    if args.verbose:
        setup_logging(verbose=True)

    schedule = config.load_schedule()
    now = now_in(schedule["timezone"])

    if not (args.dry_run or args.force) and not within_window(now, schedule):
        log.info(
            "지금은 %02d:%02d (KST) 로 알림 시간대(%02d~%02d시) 밖이라 실행하지 않습니다.",
            now.hour,
            now.minute,
            schedule["start_hour"],
            schedule["end_hour"],
        )
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    try:
        result = run(args)
    except StageFailure as exc:
        log.exception("[%s] 단계에서 오류가 발생했습니다.", exc.stage)
        text = message.build_failure_message(exc.stage, exc.original, now)
        return _send_failure(text, token, chat_id, args.dry_run)
    except Exception as exc:  # 단계 밖(설정 로딩 등)에서 난 오류
        log.exception("초기화 중 오류가 발생했습니다.")
        text = message.build_failure_message("초기화", exc, now)
        return _send_failure(text, token, chat_id, args.dry_run)

    if result is None:
        # DUMP_HTML 모드: run() 안에서 이미 발송까지 마쳤다.
        return 0

    return _send_result(result, args, now, schedule, token, chat_id)


def _send_failure(text: str, token: str, chat_id: str, dry_run: bool) -> int:
    if dry_run:
        print("\n----- 보낼 메시지 (실제로 보내지는 않음) -----")
        print(text)
        print("----- 끝 -----\n")
        return 1

    try:
        telegram.send(token, chat_id, text, silent=False)
    except Exception as exc:
        log.error("텔레그램 발송에 실패했습니다: %s", type(exc).__name__)
    return 1


def _send_result(
    result: RunResult,
    args: argparse.Namespace,
    now: datetime,
    schedule: dict[str, Any],
    token: str,
    chat_id: str,
) -> int:
    report = result.report
    quiet_hours = schedule.get("quiet_report_hours", [8, 20])

    if report.openings:
        digest = _digest(report.openings)
        if not args.dry_run and digest == _load_last_digest():
            log.info("직전 결과와 완전히 동일해 재전송을 생략합니다.")
            return 0
        text = message.build_message(report, result.endpoints, now, result.label, result.heartbeat)
        silent = False
        if not args.dry_run:
            _save_last_digest(digest)
    else:
        if now.hour not in quiet_hours:
            log.info(
                "빈자리 없음이고 무음 발송 시각(%s)이 아니라 이번엔 보내지 않습니다.",
                ", ".join(str(h) for h in quiet_hours),
            )
            return 0
        text = message.build_message(report, result.endpoints, now, result.label, result.heartbeat)
        silent = True

    if args.dry_run:
        print("\n----- 보낼 메시지 (실제로 보내지는 않음) -----")
        print(text)
        print("----- 끝 -----\n")
        return 0

    try:
        _stage("발송", telegram.send, token, chat_id, text, silent=silent)
    except StageFailure as exc:
        log.error("[%s] 텔레그램 발송에 실패했습니다: %s", exc.stage, exc.original)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
