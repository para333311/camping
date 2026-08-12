#!/usr/bin/env python3
"""숲나들e 수도권 휴양림 빈자리 텔레그램 알림봇.

하는 일
  1) 지금이 알림 시간대(KST)인지 확인한다. 아니면 아무것도 하지 않고 끝낸다.
  2) 휴양림 검색 화면(로그인 불필요, 평범한 GET)에서 휴양림 목록을 모은다.
  3) 숲나들e 에 로그인한다 (Playwright, 실행당 1회). 월별예약조회 엔드포인트를 확보한다.
  4) 조회할 숙박일(연휴 + 8주 이내 금/토)을 계산한다.
  5) 빈 객실을 모아 연박으로 묶고, 예약 화면 링크를 붙인다.
  6) 텔레그램 채널로 무조건 1건 보낸다. (빈자리가 없어도 '없음' 을 보낸다)

절대 하지 않는 일
  예약 신청, 결제, 캡차/대기열 우회, 로그인 반복 시도, 잦은 폴링.
  이 스크립트는 '조회' 와 '링크 제공' 까지만 한다.

사용법
  python scripts/notify.py             # 정상 실행 (시간대 검사 O, 발송 O)
  python scripts/notify.py --dry-run   # 시간대 검사 X, 발송 X, 결과만 화면에 출력
  python scripts/notify.py --force     # 시간대 검사 X, 발송 O (수동 실행용)
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from foresttrip import config, dateplan, forests as forests_mod, message, query, telegram
from foresttrip.diagnostics import diagnose, print_diagnostic_block
from foresttrip.logging_setup import setup_logging
from foresttrip.session import ForestSession

log = setup_logging()


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
# 엔드포인트 자동학습
# --------------------------------------------------------------------------- #

def needs_discovery(endpoints: dict[str, Any]) -> bool:
    if not endpoints.get("auto_discover", True):
        return False
    monthly = endpoints.get("monthly") or {}
    if not monthly.get("url"):
        return True
    stamp = endpoints.get("discovered_at")
    if not stamp:
        return True
    try:
        when = datetime.fromisoformat(str(stamp))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    ttl = int(endpoints.get("discovery_ttl_days", 7))
    return datetime.now(timezone.utc) - when > timedelta(days=ttl)


def run_discovery(
    session: ForestSession,
    endpoints: dict[str, Any],
    sample_forest: dict[str, Any],
    sample_date: Any,
) -> dict[str, Any]:
    spec = session.discover_monthly(str(sample_forest["insttId"]), sample_date)
    if not spec:
        if endpoints.get("monthly", {}).get("url"):
            log.warning("자동학습에 실패했지만 기존 설정이 있어 그대로 진행합니다.")
            return endpoints
        # config 파일에 아무것도 저장하지 않으므로 discovered_at 이 그대로 비어 있고,
        # 다음 실행에서 needs_discovery() 가 다시 True 를 돌려줘 자동으로 재시도된다.
        raise RuntimeError("월별예약조회 엔드포인트를 찾지 못했습니다.")

    # 응답 예시는 파일에 저장하지 않고, 어떤 키가 있었는지만 로그로 남긴다.
    sample = spec.pop("_sample_row", None)
    if sample:
        log.info("응답 필드 예시: %s", ", ".join(list(sample.keys())[:25]))

    config.save_endpoints(spec)
    merged = config.load_endpoints()
    return merged


# --------------------------------------------------------------------------- #
# 본체
# --------------------------------------------------------------------------- #

def run(args: argparse.Namespace) -> str:
    schedule = config.load_schedule()
    codes, label = config.load_regions()
    date_cfg = config.load_dates()
    endpoints = config.load_endpoints()

    now = now_in(schedule["timezone"])

    targets = dateplan.build_target_dates(date_cfg, now.date())
    if not targets:
        log.warning("조회할 날짜가 없습니다. config/dates.json 을 확인하세요.")

    # 휴양림 검색 화면은 로그인이 필요 없는 평범한 GET 이라, 브라우저를
    # 띄우기 전에 가벼운 HTTP 요청만으로 먼저 끝낸다.
    forest_list = forests_mod.get_forests(endpoints, codes)

    forest_id = os.environ.get("FOREST_ID", "")
    forest_pw = os.environ.get("FOREST_PW", "")

    with ForestSession(endpoints) as session:
        session.login(forest_id, forest_pw)

        if targets and needs_discovery(endpoints):
            endpoints = run_discovery(session, endpoints, forest_list[0], targets[0])

        if not endpoints.get("monthly", {}).get("url"):
            raise RuntimeError("엔드포인트를 찾지 못했습니다.")

        cookies = session.cookie_jar()
        headers = session.request_headers()

        if not targets:
            report = query.QueryReport(checked=len(forest_list), failed=0)
        else:
            try:
                report = query.query_all(forest_list, endpoints, targets, cookies, headers)
            except query.AuthExpired as exc:
                # 세션이 끊긴 경우에만 딱 한 번 다시 로그인한다.
                log.warning("세션이 만료되어 1회 재로그인 후 다시 시도합니다. (%s)", exc)
                session.login(forest_id, forest_pw)
                report = query.query_all(
                    forest_list, endpoints, targets, session.cookie_jar(), session.request_headers()
                )

    heartbeat = now.hour == schedule["heartbeat_hour"]
    return message.build_message(report, endpoints, now, label, heartbeat)


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

    # 조회 자체가 실패했는지 기록해둔다. 텔레그램 알림은 실패해도 무조건
    # 보내지만(사용자는 로그를 못 보니 텔레그램이 유일한 창구), Actions 의
    # 성공/실패 표시는 실제 결과를 반영해야 하므로 마지막에 이 값으로 종료
    # 코드를 정한다. 참고로 GitHub 은 '연속 실패'가 아니라 '60일간 커밋이
    # 없을 때'만 schedule 을 끄므로, 실패를 있는 그대로 표시해도
    # keepalive.yml(매주 커밋)이 있는 한 스케줄이 꺼지지 않는다.
    had_error = False

    try:
        text = run(args)
        silent = "예약 가능 객실 없음" in text
    except Exception as exc:
        # 실패 원인은 config 파일에 저장되지 않으므로, 다음 시간(스케줄이면 1시간 뒤)
        # 실행에서 자동으로 다시 시도된다. 사용자가 파일을 손으로 고칠 필요는 없다.
        log.exception("조회 중 오류가 발생했습니다.")
        had_error = True
        diagnosis = diagnose(exc)
        print_diagnostic_block(diagnosis, exc)
        text = message.build_failure_message(diagnosis.short, now)
        silent = False

    if args.dry_run:
        print("\n----- 보낼 메시지 (실제로 보내지는 않음) -----")
        print(text)
        print("----- 끝 -----\n")
        return 1 if had_error else 0

    try:
        telegram.send(token, chat_id, text, silent=silent)
    except Exception as exc:
        # 텔레그램 발송 자체가 실패하면 사용자에게 아무 알림도 가지 않으므로
        # 이것도 실패로 취급한다. 다만 워크플로가 예외로 죽지는 않게 한다.
        log.error("텔레그램 발송에 실패했습니다: %s", type(exc).__name__)
        had_error = True

    return 1 if had_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
