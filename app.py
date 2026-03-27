"""
영수증 자동화 파이프라인 서버
- 아이폰 단축어 → 이 서버 → OCR(Claude Vision) → Slack Webhook + Google Sheets API + Drive API
외부 의존성 없이 순수 HTTP API만 사용 (Render 환경 호환)
"""

import os
import json
import base64
import asyncio
import aiohttp
from datetime import datetime
from flask import Flask, request, jsonify
import anthropic

app = Flask(__name__)

# ── 환경변수 ──────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
SLACK_BOT_TOKEN    = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_USER_ID      = os.environ.get("SLACK_USER_ID", "U03QB1DK12L")
SHEETS_ID          = os.environ.get("SHEETS_ID", "12h48_MBGeRmGXP2exEY3fNFAPxYdnnAqZc6T8VbnBp0")
GOOGLE_TOKEN       = os.environ.get("GOOGLE_ACCESS_TOKEN", "")   # 주기적 갱신 필요 or Service Account
WEBHOOK_SECRET     = os.environ.get("WEBHOOK_SECRET", "sandbox2026")
DRIVE_FOLDER_ID    = os.environ.get("DRIVE_FOLDER_ID", "")


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
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


# ── Slack: Bot Token으로 DM 전송 ─────────────────────────
async def send_slack_dm(session: aiohttp.ClientSession, text: str):
    if not SLACK_BOT_TOKEN:
        print("[Slack] No token, skipping")
        return
    payload = {"channel": SLACK_USER_ID, "text": text}
    async with session.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}", "Content-Type": "application/json"},
        json=payload,
    ) as resp:
        data = await resp.json()
        if not data.get("ok"):
            print(f"[Slack error] {data.get('error')}")


# ── Sheets: Service Account or OAuth token으로 행 추가 ───
async def append_to_sheets(session: aiohttp.ClientSession, ocr: dict, drive_url: str) -> str:
    if not GOOGLE_TOKEN:
        print("[Sheets] No token, skipping")
        return ""

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    total   = int(ocr.get("합계금액", 0))
    vat     = int(ocr.get("VAT", 0))
    supply  = int(ocr.get("공급가액", 0))

    anomaly = ""
    if total > 500000:
        anomaly += f"고액({total:,}원)"
    if supply > 0:
        ratio = vat / supply
        if not (0.08 <= ratio <= 0.12):
            anomaly += f" VAT비율이상({ratio:.1%})"

    row = [[
        ocr.get("날짜", ""),
        ocr.get("공급처", ""),
        supply,
        vat,
        total,
        ocr.get("카테고리", ""),
        ocr.get("메모", ""),
        drive_url,
        now_str,
        anomaly.strip(),
    ]]

    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEETS_ID}/values/A1:append?valueInputOption=USER_ENTERED"
    async with session.post(
        url,
        headers={"Authorization": f"Bearer {GOOGLE_TOKEN}", "Content-Type": "application/json"},
        json={"values": row},
    ) as resp:
        if resp.status != 200:
            print(f"[Sheets error] {resp.status}: {await resp.text()}")

    return anomaly.strip()


# ── Drive: 파일 업로드 ────────────────────────────────────
async def upload_to_drive(session: aiohttp.ClientSession, image_bytes: bytes, filename: str, folder_id: str) -> str:
    if not GOOGLE_TOKEN:
        print("[Drive] No token, skipping")
        return ""

    metadata = {"name": filename}
    if folder_id:
        metadata["parents"] = [folder_id]

    # multipart 업로드
    import aiohttp
    data = aiohttp.FormData()
    data.add_field("metadata", json.dumps(metadata), content_type="application/json")
    data.add_field("file", image_bytes, content_type="image/jpeg", filename=filename)

    async with session.post(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,webViewLink",
        headers={"Authorization": f"Bearer {GOOGLE_TOKEN}"},
        data=data,
    ) as resp:
        if resp.status == 200:
            result = await resp.json()
            return result.get("webViewLink", result.get("id", ""))
        else:
            print(f"[Drive error] {resp.status}: {await resp.text()}")
            return ""


# ── 메인 처리 ─────────────────────────────────────────────
async def process_receipt(image_bytes: bytes, media_type: str, filename: str, year_month: str):
    # 1. OCR
    try:
        ocr = ocr_receipt(image_bytes, media_type)
    except Exception as e:
        return {"status": "error", "step": "ocr", "message": str(e)}

    total  = int(ocr.get("합계금액", 0))
    vat    = int(ocr.get("VAT", 0))
    supply = int(ocr.get("공급가액", 0))

    # 이상치 플래그
    anomaly_flag = ""
    if total > 500000:
        anomaly_flag += " ⚠️ 고액"
    if supply > 0 and not (0.08 <= vat / supply <= 0.12):
        anomaly_flag += " ⚠️ VAT비율이상"

    slack_text = (
        f"🧾 *영수증 접수*{anomaly_flag}\n"
        f"> 날짜: {ocr.get('날짜', '-')}\n"
        f"> 공급처: *{ocr.get('공급처', '-')}*\n"
        f"> 공급가액: {supply:,}원  |  VAT: {vat:,}원  |  *합계: {total:,}원*\n"
        f"> 카테고리: {ocr.get('카테고리', '-')}\n"
        f"> 메모: {ocr.get('메모', '-') or '없음'}"
    )

    # 2. Drive 폴더 결정 (월별)
    folder_id = DRIVE_FOLDER_ID  # 없으면 루트에 업로드

    async with aiohttp.ClientSession() as session:
        # Drive + Slack + Sheets 병렬 처리
        drive_task  = asyncio.create_task(upload_to_drive(session, image_bytes, filename, folder_id))
        slack_task  = asyncio.create_task(send_slack_dm(session, slack_text))

        drive_url = await drive_task
        await slack_task

        ocr["drive_url"] = drive_url
        await append_to_sheets(session, ocr, drive_url)

    return {
        "status": "ok",
        "공급처": ocr.get("공급처"),
        "합계금액": total,
        "날짜": ocr.get("날짜"),
        "카테고리": ocr.get("카테고리"),
        "drive_url": drive_url,
    }


# ── 엔드포인트 ────────────────────────────────────────────
@app.route("/receipt", methods=["POST"])
def receive_receipt():
    # 인증
    secret = request.headers.get("X-Secret") or request.form.get("secret")
    if secret != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    # 이미지 수신
    image_bytes = None
    media_type  = "image/jpeg"
    now         = datetime.now()

    if "image" in request.files:
        f = request.files["image"]
        image_bytes = f.read()
        ext = (f.filename or "").rsplit(".", 1)[-1].lower()
        if ext in ("png",):
            media_type = "image/png"
        elif ext in ("webp",):
            media_type = "image/webp"
    elif request.data:
        image_bytes = request.data

    if not image_bytes:
        return jsonify({"error": "No image provided"}), 400

    filename   = now.strftime("receipt_%Y%m%d_%H%M%S.jpg")
    year_month = now.strftime("%Y-%m")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            process_receipt(image_bytes, media_type, filename, year_month)
        )
    finally:
        loop.close()

    return jsonify(result)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "receipt-automation"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
