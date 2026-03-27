"""
매주 금요일: 누락 영수증 + 금액 이상치 보고
Cron: 0 9 * * 5  (UTC 09:00 금요일 = KST 18:00 금요일)
"""

import os
import json
import asyncio
from datetime import datetime, timedelta


async def call_tool(source_id: str, tool_name: str, arguments: dict):
    import subprocess
    proc = await asyncio.create_subprocess_exec(
        "external-tool", "call",
        json.dumps({"source_id": source_id, "tool_name": tool_name, "arguments": arguments}),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode())
    return json.loads(stdout.decode())


async def get_weekly_receipts(sheets_id: str) -> list[dict]:
    """이번 주(월~금) 데이터 가져오기"""
    today = datetime.now()
    week_start = today - timedelta(days=today.weekday())  # 이번주 월요일
    start_str = week_start.strftime("%Y-%m-%d")

    result = await call_tool("google_sheets__pipedream", "google_sheets-get-values-in-range", {
        "sheetId": sheets_id,
        "range": "A2:J2000",  # 헤더 제외
    })

    rows = []
    raw_values = []

    if isinstance(result, dict):
        # values 키 또는 result.result
        raw = result.get("result") or result
        if isinstance(raw, dict):
            raw_values = raw.get("values", [])
        elif isinstance(raw, list):
            raw_values = raw

    for row in raw_values:
        if len(row) < 5:
            continue
        date_str = row[0] if row else ""
        if date_str >= start_str:
            rows.append({
                "날짜": row[0] if len(row) > 0 else "",
                "공급처": row[1] if len(row) > 1 else "",
                "공급가액": int(row[2]) if len(row) > 2 and row[2] else 0,
                "VAT": int(row[3]) if len(row) > 3 and row[3] else 0,
                "합계금액": int(row[4]) if len(row) > 4 and row[4] else 0,
                "카테고리": row[5] if len(row) > 5 else "",
                "이상치": row[9] if len(row) > 9 else "",
            })
    return rows


def detect_anomalies(rows: list[dict]) -> list[dict]:
    """이상치 탐지"""
    anomalies = []

    # 1. 금액 이상치: 합계금액 기준 IQR or 단순 임계값
    amounts = [r["합계금액"] for r in rows if r["합계금액"] > 0]
    if amounts:
        avg = sum(amounts) / len(amounts)
        threshold = max(avg * 3, 500000)  # 평균의 3배 또는 50만원 초과
        for r in rows:
            flags = []
            if r["합계금액"] > threshold:
                flags.append(f"고액({r['합계금액']:,}원 > 임계{int(threshold):,}원)")
            # VAT 비율 이상
            if r["공급가액"] > 0:
                ratio = r["VAT"] / r["공급가액"]
                if not (0.08 <= ratio <= 0.12):
                    flags.append(f"VAT비율이상({ratio:.1%})")
            if flags:
                r["_flags"] = " / ".join(flags)
                anomalies.append(r)

    # 2. 누락 가능성: 영업일 기준 하루에 0건이면 누락 의심
    today = datetime.now()
    week_start = today - timedelta(days=today.weekday())
    missing_days = []
    for i in range(5):  # 월~금
        day = week_start + timedelta(days=i)
        if day.date() > today.date():
            break
        day_str = day.strftime("%Y-%m-%d")
        day_rows = [r for r in rows if r["날짜"] == day_str]
        if len(day_rows) == 0:
            missing_days.append(day_str)

    return anomalies, missing_days


async def send_weekly_report():
    sheets_id   = os.environ.get("SHEETS_ID", "12h48_MBGeRmGXP2exEY3fNFAPxYdnnAqZc6T8VbnBp0")
    slack_user  = os.environ.get("SLACK_USER_ID", "U03QB1DK12L")
    sheets_url  = f"https://docs.google.com/spreadsheets/d/{sheets_id}"

    rows = await get_weekly_receipts(sheets_id)
    anomalies, missing_days = detect_anomalies(rows)

    today = datetime.now()
    week_start = today - timedelta(days=today.weekday())
    week_label = f"{week_start.strftime('%m/%d')} ~ {today.strftime('%m/%d')}"

    # 요약 통계
    total_amount = sum(r["합계금액"] for r in rows)
    total_vat    = sum(r["VAT"] for r in rows)
    count        = len(rows)

    # 리포트 메시지 구성
    lines = [
        f"📊 *주간 영수증 리포트* ({week_label})",
        f"",
        f"*이번 주 요약*",
        f"> 건수: {count}건 | 합계: {total_amount:,}원 | VAT: {total_vat:,}원",
        f"",
    ]

    if missing_days:
        lines.append(f"⚠️ *누락 의심 날짜* ({len(missing_days)}일)")
        for d in missing_days:
            lines.append(f">  • {d} — 영수증 없음")
        lines.append("")

    if anomalies:
        lines.append(f"🚨 *금액 이상치* ({len(anomalies)}건)")
        for a in anomalies:
            lines.append(f">  • {a['날짜']} | {a['공급처']} | {a['합계금액']:,}원 | {a.get('_flags','')}")
        lines.append("")
    else:
        lines.append("✅ 이상치 없음")
        lines.append("")

    lines.append(f"📋 <{sheets_url}|Google Sheets 바로가기>")

    message = "\n".join(lines)

    await call_tool("slack_direct", "slack_send_message", {
        "channel_id": slack_user,
        "text": message,
    })

    print(f"[Weekly Report] 전송 완료 — {count}건, 이상치 {len(anomalies)}건, 누락 {len(missing_days)}일")


if __name__ == "__main__":
    asyncio.run(send_weekly_report())
