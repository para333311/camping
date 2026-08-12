"""오류를 사람이 읽을 수 있는 한국어 진단으로 바꾼다.

사용자가 코딩을 모르는 초보이므로, 오류가 나면
  1) 텔레그램에는 짧은 한 줄 사유
  2) 워크플로 로그에는 그대로 복사해 전달할 수 있는 상세 진단 블록
을 남긴다. 파일을 직접 고치라는 안내는 하지 않는다 — 실패한 학습은
config/endpoints.json 이 갱신되지 않은 채로 남아 다음 시간에 자동으로
다시 시도되기 때문이다.
"""

from __future__ import annotations

import traceback
from typing import NamedTuple


class Diagnosis(NamedTuple):
    short: str   # 텔레그램에 보내는 한 줄 요약
    detail: str  # 로그에 남기는 상세 설명 (복사해서 전달 가능)


def diagnose(exc: BaseException) -> Diagnosis:
    from .forests import FOREST_COLLECT_FAILED_MSG
    from .query import AuthExpired
    from .session import LoginError

    name = type(exc).__name__
    text = str(exc)
    lname = name.lower()

    if isinstance(exc, LoginError):
        if "입력칸을 찾지 못했습니다" in text:
            return Diagnosis(
                "로그인 화면 구조가 바뀐 것 같습니다 (아이디/비밀번호 입력칸을 못 찾음)",
                "숲나들e 로그인 화면의 생김새(HTML 구조)가 바뀌어서, 봇이 아이디와 비밀번호를 "
                "입력할 칸을 자동으로 찾지 못했습니다. 아이디/비밀번호 자체는 틀리지 않았을 "
                "가능성이 높습니다. 다음 시간에 자동으로 다시 시도합니다. 같은 오류가 계속되면 "
                "숲나들e 사이트가 개편된 것이니, 이 진단 블록을 그대로 개발자에게 전달해 주세요."
            )
        return Diagnosis(
            "로그인에 실패했습니다 (아이디/비밀번호를 확인해 주세요)",
            "숲나들e 로그인이 실패했습니다. 원인일 수 있는 것들: "
            "① GitHub 저장소의 FOREST_ID / FOREST_PW 값이 실제 로그인 정보와 다름, "
            "② 최근 숲나들e 비밀번호를 바꾸었는데 GitHub Secrets 에는 이전 값이 남아 있음, "
            "③ 숲나들e 에서 로그인 실패가 누적되어 잠시 접속이 제한됨, "
            "④ 사이트가 캡차 등 추가 인증을 요구하기 시작함. "
            "GitHub 저장소 화면 위쪽 Settings 탭 → 왼쪽 메뉴 Secrets and variables → Actions "
            "에서 FOREST_ID, FOREST_PW 값을 다시 등록해 보시고, 그래도 안 되면 숲나들e 사이트에 "
            "직접 로그인해서 비밀번호나 계정 상태를 확인해 주세요."
        )

    if isinstance(exc, AuthExpired):
        return Diagnosis(
            "조회 중 로그인이 자꾸 풀렸습니다",
            "로그인 자체는 성공했지만, 객실을 조회하는 도중 세션이 반복해서 끊겼습니다 "
            "(서버가 401 이나 로그인 안내 화면을 돌려줌). 숲나들e 서버가 일시적으로 "
            "불안정하거나 점검 중일 가능성이 있습니다. 다음 시간에 자동으로 다시 시도합니다."
        )

    if "엔드포인트를 찾지 못했습니다" in text:
        return Diagnosis(
            "숲나들e 응답 형식을 자동으로 알아내지 못했습니다",
            "예약 화면을 열어서 객실 목록이 담긴 응답(JSON)을 찾아보았지만 찾지 못했습니다. "
            "원인일 수 있는 것들: ① 숲나들e 화면 구조가 바뀌어 봇이 조회 버튼을 못 눌렀음, "
            "② 응답이 JSON 이 아니라 안내 화면(HTML)으로 왔음(로그인이 실제로는 안 된 상태), "
            "③ 사이트가 일시적으로 느려서 시간 안에 응답을 받지 못함. "
            "이 실패는 config 파일에 저장되지 않으므로 다음 시간에 자동으로 다시 시도합니다. "
            "3~4회 이상 같은 오류가 반복되면 사이트가 개편된 것이니 이 진단 블록을 그대로 "
            "개발자에게 전달해 주세요."
        )

    # 정확한 문구가 아니라 핵심 낱말(수집/휴양림)로 느슨하게 맞춰본다.
    # forests.py 가 실제로 던지는 문장은 FOREST_COLLECT_FAILED_MSG 상수와
    # 같지만, 문구가 살짝 바뀌어도 이 분류가 깨지지 않도록 이중으로 검사한다.
    if (
        FOREST_COLLECT_FAILED_MSG in text
        or ("휴양림" in text and "수집" in text)
        or "조회할 휴양림이 없습니다" in text
    ):
        return Diagnosis(
            "휴양림 목록을 가져오지 못했습니다",
            "숲나들e 의 휴양림 검색 화면에서 휴양림 이름과 번호를 하나도 읽어오지 못했습니다. "
            "검색 화면 구조가 바뀌었을 가능성이 있습니다. 다음 시간에 자동으로 다시 시도합니다.\n"
            f"봇이 시도해 본 방법과 그 결과: {text}"
        )

    if "json" in lname or "jsondecodeerror" in lname:
        return Diagnosis(
            "숲나들e 응답을 이해하지 못했습니다",
            "사이트에서 예상과 다른 형식의 응답이 와서 처리하지 못했습니다. "
            "일시적인 오류이거나 사이트 개편일 수 있습니다. 다음 시간에 자동으로 다시 시도합니다."
        )

    if "timeout" in lname or "connect" in lname or "network" in lname or "playwright" in lname:
        return Diagnosis(
            "숲나들e 사이트 접속이 지연되거나 실패했습니다",
            "숲나들e 사이트에 접속하는 중 시간이 오래 걸리거나 연결이 끊겼습니다. "
            "사이트 점검 중이거나 일시적인 네트워크 문제로 보입니다. "
            "다음 시간에 자동으로 다시 시도합니다."
        )

    return Diagnosis(
        f"예상하지 못한 오류가 발생했습니다 ({name})",
        f"분류되지 않은 오류입니다. 오류 종류: {name}, 내용: {text}\n"
        "다음 시간에 자동으로 다시 시도합니다. 같은 오류가 반복되면 이 진단 블록을 "
        "그대로 개발자에게 전달해 주세요."
    )


def _traceback_tail(exc: BaseException, lines: int = 3) -> str:
    """traceback 마지막 몇 줄만 뽑아 요약한다. 개발자가 어느 코드 줄에서
    터졌는지 바로 짚을 수 있게 하기 위함이다."""
    try:
        formatted = traceback.format_exception(type(exc), exc, exc.__traceback__)
        joined = "".join(formatted).rstrip("\n").split("\n")
        return "\n".join(joined[-lines:])
    except Exception:
        return f"{type(exc).__name__}: {exc}"


def print_diagnostic_block(diagnosis: Diagnosis, exc: BaseException) -> None:
    """워크플로 로그 맨 아래에 복사해서 전달 가능한 진단 블록을 출력한다.

    이 함수를 부르는 시점엔 이미 stdout 이 비밀값 마스킹 스트림으로 감싸여 있으므로
    (logging_setup.setup_logging), 여기서 만드는 텍스트도 자동으로 마스킹된다.
    """
    print("\n" + "=" * 64)
    print("📋 아래 내용을 그대로 복사해서 전달하면 원인 파악에 도움이 됩니다")
    print("=" * 64)
    print(f"[요약] {diagnosis.short}")
    print(f"[상세] {diagnosis.detail}")
    print(f"[오류 종류] {type(exc).__name__}")
    print(f"[traceback 마지막 {3}줄]\n{_traceback_tail(exc)}")
    print("=" * 64 + "\n")
