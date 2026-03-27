"""
영수증 자동화 파이프라인 서버
- 아이폰 단축어 → 이 서버 → OCR(Claude Vision) → Slack DM + Google Sheets + Drive
"""

import os
import json
import base64
import asyncio
import tempfile
import subprocess
from datetime import datetime
from flask import Flask, request, jsonify
import anthropic

app = Flask(__name__)

# ── 환경변수 ──────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SLACK_USER_ID     = os.environ.get("SLACK_USER_ID", "U03QB1DK12L")  # 개인 DM (자기자신)
SHEETS_ID         = os.environ.get("SHEETS_ID", "12h48_MBGeRmGXP2exEY3fNFAPxYdnnAqZc6T8VbnBp0")
DRIVE_ROOT_FOLDER = os.environ.get("DRIVE_ROOT_FOLDER", "")          # 루트 폴더 ID (선택)
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "sandbox2026")  # 단순 인증 토큰

# ── 외부 툴 호출 헬퍼 ─────────────────────────────────────
async def call_tool(source_id: str, tool_name: str, arguments: dict):
    proc = await asyncio.create_subprocess_exec(
        "external-tool", "call",
        json.dumps({"source_id": source_id, "tool_name": tool_name, "arguments": arguments}),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode()
        print(f"[TOOL ERROR] {tool_name}: {err}")
        raise RuntimeError(err)
    return json.loads(stdout.decode())


# ── OCR: Claude Vision ────────────────────────────────────
def ocr_receipt(image_bytes: bytes, media_type: str = "image/jpeg") -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    prompt = """이 영수증 이미지를 분석해서 아래 정보를 JSON으로만 응답해줘.
다른 텍스트 없이 JSON만 출력해.

{
  "공급처": "상호명 또는 공급자명",
  "공급가액": 숫자(원, 정수),
  "VAT": 숫자(원, 정수),
  "합계금액": 숫자(원, 정수),
  "날짜": "YYYY-MM-DD",
  "카테고리": "식비/교통비/소모품/접대비/기타 중 하나",
  "메모": "영수증에서 주목할 만한 추가 정보 (없으면 빈 문자열)"
}

- VAT가 명시되지 않은 경우: 합계금액 기준 10/110 으로 추정
- 날짜가 없으면 오늘 날짜 사용
- 숫자는 콤마 없이 정수만"""

    resp = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    raw = resp.content[0].text.strip()
    # JSON 블록 파싱
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


# ── Drive: 월별 폴더 확인/생성 후 파일 업로드 ──────────────
async def upload_to_drive(image_bytes: bytes, filename: str, year_month: str) -> str:
    """
    Drive 구조: 영수증_자동화/ > 2026-03/ > receipt_20260327_143022.jpg
    실제 파일 업로드는 export_files(workspace 파일 경로) 방식을 사용
    """
    # 임시 파일로 저장
    tmp_path = f"/tmp/{filename}"
    with open(tmp_path, "wb") as f:
        f.write(image_bytes)

    # external-tool export_files 는 workspace 경로가 필요 → /tmp → /home/user/workspace 로 복사
    ws_dir = f"/home/user/workspace/uploads/{year_month}"
    os.makedirs(ws_dir, exist_ok=True)
    ws_path = f"{ws_dir}/{filename}"

    import shutil
    shutil.copy(tmp_path, ws_path)

    # Drive에 업로드
    result = await call_tool("google_drive", "export_files", {"file_paths": [ws_path]})
    # 반환값에서 URL 추출 (있으면)
    drive_url = ""
    if isinstance(result, dict):
        drive_url = result.get("url") or result.get("webViewLink") or result.get("id") or ""
    elif isinstance(result, list) and result:
        item = result[0]
        drive_url = item.get("url") or item.get("webViewLink") or item.get("id") or ""

    return drive_url


# ── Slack: 개인 DM 메시지 전송 ───────────────────────────
async def send_slack_message(text: str):
    await call_tool("slack_direct", "slack_send_message", {
        "channel_id": SLACK_USER_ID,
        "text": text,
    })


# ── Sheets: 행 추가 ───────────────────────────────────────
async def append_to_sheets(row_data: dict):
    """
    컬럼 순서: 날짜 | 공급처 | 공급가액 | VAT | 합계금액 | 카테고리 | 메모 | Drive URL | Slack전송시각 | 이상치
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 이상치 판정 (합계금액 > 500,000 원 또는 VAT 비율 비정상)
    total = int(row_data.get("합계금액", 0))
    vat   = int(row_data.get("VAT", 0))
    supply = int(row_data.get("공급가액", 0))
    anomaly = ""
    if total > 500000:
        anomaly = f"고액({total:,}원)"
    if supply > 0:
        vat_ratio = vat / supply
        if not (0.08 <= vat_ratio <= 0.12):
            anomaly += f" VAT비율이상({vat_ratio:.1%})"

    rows = [[
        row_data.get("날짜", ""),
        row_data.get("공급처", ""),
        supply,
        vat,
        total,
        row_data.get("카테고리", ""),
        row_data.get("메모", ""),
        row_data.get("drive_url", ""),
        now_str,
        anomaly.strip(),
    ]]

    await call_tool("google_sheets__pipedream", "google_sheets-add-multiple-rows", {
        "sheetId": SHEETS_ID,
        "worksheetId": 0,
        "rows": json.dumps(rows),
    })
    return anomaly.strip()


# ── 메인 엔드포인트 ───────────────────────────────────────
@app.route("/receipt", methods=["POST"])
def receive_receipt():
    # 간단 인증
    secret = request.headers.get("X-Secret") or request.form.get("secret")
    if secret != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    # 이미지 수신 (multipart/form-data 또는 raw binary)
    image_bytes = None
    media_type  = "image/jpeg"
    filename_prefix = datetime.now().strftime("receipt_%Y%m%d_%H%M%S")

    if "image" in request.files:
        f = request.files["image"]
        image_bytes = f.read()
        ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else "jpg"
        media_type  = f"image/{ext}" if ext in ("jpg","jpeg","png","webp","heic") else "image/jpeg"
    elif request.data:
        image_bytes = request.data
    else:
        return jsonify({"error": "No image provided"}), 400

    year_month = datetime.now().strftime("%Y-%m")
    filename   = f"{filename_prefix}.jpg"

    # 비동기 파이프라인 실행
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            process_receipt(image_bytes, media_type, filename, year_month)
        )
    finally:
        loop.close()

    return jsonify(result)


async def process_receipt(image_bytes, media_type, filename, year_month):
    # 1. OCR
    try:
        ocr = ocr_receipt(image_bytes, media_type)
    except Exception as e:
        return {"status": "error", "step": "ocr", "message": str(e)}

    # 2. Drive 업로드 (병렬 처리를 위해 먼저 시작)
    drive_task = asyncio.create_task(
        upload_to_drive(image_bytes, filename, year_month)
    )

    # 3. Slack 메시지 구성
    anomaly_flag = ""
    total = int(ocr.get("합계금액", 0))
    vat   = int(ocr.get("VAT", 0))
    supply = int(ocr.get("공급가액", 0))
    if total > 500000:
        anomaly_flag = " ⚠️ 고액"
    if supply > 0 and not (0.08 <= vat/supply <= 0.12):
        anomaly_flag += " ⚠️ VAT비율이상"

    slack_text = (
        f"🧾 *영수증 접수*{anomaly_flag}\n"
        f"> 날짜: {ocr.get('날짜', '-')}\n"
        f"> 공급처: *{ocr.get('공급처', '-')}*\n"
        f"> 공급가액: {supply:,}원 | VAT: {vat:,}원 | *합계: {total:,}원*\n"
        f"> 카테고리: {ocr.get('카테고리', '-')}\n"
        f"> 메모: {ocr.get('메모', '-') or '없음'}"
    )

    # 4. Drive 결과 기다림
    try:
        drive_url = await drive_task
    except Exception as e:
        print(f"[Drive error] {e}")
        drive_url = ""

    ocr["drive_url"] = drive_url

    # 5. Sheets 기록 & Slack 전송 (병렬)
    sheets_task = asyncio.create_task(append_to_sheets(ocr))
    slack_task  = asyncio.create_task(send_slack_message(slack_text))
    await asyncio.gather(sheets_task, slack_task, return_exceptions=True)

    return {
        "status": "ok",
        "공급처": ocr.get("공급처"),
        "합계금액": total,
        "날짜": ocr.get("날짜"),
        "drive_url": drive_url,
    }


# ── 헬스체크 ─────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "receipt-automation"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
