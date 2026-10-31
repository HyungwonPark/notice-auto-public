"""Korean wording for everything the pages show. Stored values stay stable codes; only the text changes here."""

CATEGORY = {
    "kt_notice": "KT 작업 공지",
    "kt_urgent": "KT 긴급 작업 공지",
    "kt_complete": "KT 작업 완료 공지",
    "kt_cancel": "KT 작업 취소/변경 공지",
    "kt_incident": "KT 장애 발생 공지",
    "kt_recovery": "KT 장애 복구 공지",
    "kt_postpone": "KT 작업 연기 공지",
    "kt_check": "KT 서비스 점검 요청 공지",
    "kt_other": "KT 기타 메일",
    "nc24_notice": "보낸 사전 공지 메일",
    "nc24_complete": "보낸 완료 공지 메일",
    "nc24_postpone": "보낸 연기 공지 메일",
    "partner_notice": "별도 발송 사전 공지 메일",
    "partner_complete": "별도 발송 완료 공지 메일",
    "suspect": "보낸 사람 확인 필요",
    "other": "기타 메일",
    "duplicate": "이미 받은 메일",
}

# Notification.review_reasons
REASON = {
    "notice needs review": "KT 공지 내용 중 자동으로 읽지 못한 부분이 있습니다",
    "account not in customer master": "고객 정보에 없는 계정입니다",
    "no email address in customer master": "고객 정보에 이메일이 없습니다",
    "notice lists several accounts; check the quote": "공지에 여러 계정이 있습니다. 다른 고객 내용이 섞이지 않았는지 확인하세요",
    "KT sent a different notice for the same work": "KT가 같은 작업에 내용이 다른 공지를 다시 보냈습니다",
    "KT change/cancel notice received": "KT 작업 취소/변경 공지가 왔습니다",
    "several notifications match one copy": "보낸 메일 하나가 여러 안내와 겹칩니다",
    "server not in any group": "그룹에 없는 서버가 있어 전체 받는 사람에게 보냅니다",
    "copy To/Cc has none of the master emails": "보낸 메일의 받는 사람에 고객 이메일이 없습니다",
    "number matches but account differs": "안내번호는 같지만 대상 계정이 다릅니다",
    "sending failed 3 times": "자동 발송이 3번 연속 실패했습니다. 확인 후 '발송하기'를 누르세요",
    "SENS rejected the mail": "SENS가 메일을 거절했습니다. 받는 사람과 내용을 확인하세요",
    "SENS could not deliver": "SENS가 메일을 전달하지 못했습니다",
    "some recipients failed (SENS)": "일부 받는 사람에게 전달되지 않았습니다",
    "SENS has the same mail": "받는 사람과 제목이 같은 메일이 이미 SENS로 나갔습니다. 다른 안내의 메일이었다면 '발송하기'를 누르세요",
}

# Notice.problems
PROBLEM = {
    "no 작업일시": "작업 일시가 없습니다",
    "unparsed 작업일시": "작업 일시를 읽지 못했습니다",
    "no 대상계정명": "대상 계정이 없습니다",
    "no 작업내용": "작업 내용이 없습니다",
    "대상계정명 without an email address": "대상 계정에 이메일 주소가 없습니다",
    "invalid date or time in 작업일시": "작업 일시의 날짜나 시간이 올바르지 않습니다",
    "작업일시 ends before it starts": "작업 종료 시각이 시작보다 빠릅니다",
    "작업일시 start and end times are equal": "작업 시작과 종료 시각이 같습니다",
    "작업일시 duration note disagrees with the times": "괄호 안의 작업 시간과 실제 시각이 다릅니다",
    "작업 연기 공지 without a matching work": "연기한 작업을 찾지 못했습니다",
}

# CustomerAccount.problems
CUSTOMER_PROBLEM = {
    "no account": "계정이 비어 있습니다",
    "account is not an email address": "계정이 이메일 형식이 아닙니다",
    "no email address": "이메일이 없습니다",
    "email cell has characters besides addresses": "이메일 칸에 주소가 아닌 문자가 섞여 있습니다",
    "no 회사명 or name": "회사명과 이름이 모두 비어 있습니다",
}

# views._work_state: the colour of a work's dot and calendar chip, spelled out for screen readers
WORK_STATE = {
    "done": "사전 공지와 완료 공지 모두 확인",
    "sent": "발송 확인, 담당자 확인 남음",
    "waiting": "완료 공지 기다리는 중",
    "pending": "발송 대기",
    "warn": "확인 필요",
    "internal": "사내/안 쓰는 계정",
}


def translate(text: str, table: dict[str, str]) -> list[str]:
    out = []
    for line in (text or "").split("\n"):
        if line:
            if line.startswith("conflicting "):
                out.append("같은 항목이 공지 안에 서로 다르게 적혀 있습니다")
            else:
                out.append(table.get(line, line))
    return out
