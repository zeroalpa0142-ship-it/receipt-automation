# 영수증 자동화 파이프라인

아이폰에서 영수증 사진을 찍으면 → OCR(Claude Vision) → Slack 개인 DM + Google Sheets 기록 + Drive 월별 정리 → 매주 금요일 이상치 보고

---

## 아키텍처

```
📱 아이폰 단축어
    └─ 사진 촬영/선택
    └─ HTTP POST multipart/form-data
           │
           ▼
🖥️  Flask 서버 (Render 배포)
    ├─ Claude Vision OCR → 공급처, 공급가액, VAT 추출
    ├─ Google Drive → 영수증_자동화/2026-03/receipt_XXX.jpg 저장
    ├─ Google Sheets → 행 추가 (날짜, 공급처, 금액, VAT, 카테고리...)
    └─ Slack DM → 실시간 알림
           │
           ▼ (매주 금요일 18:00 KST)
📊 주간 리포트 (weekly_report.py)
    └─ 누락 날짜 + 이상치 → Slack DM 보고
```

---

## 1. 환경 설정

### 필요한 값
| 변수 | 설명 |
|------|------|
| `ANTHROPIC_API_KEY` | Anthropic Console에서 발급 |
| `SHEETS_ID` | 이미 생성됨: `12h48_MBGeRmGXP2exEY3fNFAPxYdnnAqZc6T8VbnBp0` |
| `SLACK_USER_ID` | `U03QB1DK12L` (taesup 본인 계정) |
| `WEBHOOK_SECRET` | 단축어 인증 토큰 (원하는 값으로 변경) |

---

## 2. Render 배포

1. GitHub 리포 생성 후 이 폴더 전체 푸시
2. [render.com](https://render.com) → New → Blueprint → 이 리포 연결
3. 환경변수 설정:
   - `ANTHROPIC_API_KEY` → Anthropic Console 키
   - `WEBHOOK_SECRET` → 원하는 비밀 토큰 (예: `sandbox2026`)
4. Deploy → 서버 URL 확인 (예: `https://receipt-automation.onrender.com`)

> ⚠️ Render 무료 플랜은 15분 비활성시 슬립 → 첫 요청이 30초 걸릴 수 있음.
> 무료 업타임 서비스([cron-job.org](https://cron-job.org))로 10분마다 `/health` 핑 설정 추천.

---

## 3. iOS 단축어 설정

### 단축어 구성 (단계별)

**Step 1: 단축어 앱 실행**
→ 우측 상단 `+` 탭 → 이름: "영수증 스캔"

**Step 2: 액션 추가**

| # | 액션 | 설정값 |
|---|------|--------|
| 1 | **문서 스캔** | 스캔 방식: 카메라 (또는 "사진 선택"으로 갤러리에서도 가능) |
| 2 | **URL 내용 가져오기** | 아래 참고 |

**Step 2의 URL 내용 가져오기 설정:**
```
URL: https://receipt-automation.onrender.com/receipt
메서드: POST
요청 본문: Form
헤더 추가: X-Secret → sandbox2026 (WEBHOOK_SECRET 값)

Form 필드:
  - 이름: image
    값: [Step 1의 스캔한 PDF/이미지 변수]
```

> 📌 팁: 단축어 홈 화면에서 `...` → 홈 화면에 추가 → 아이콘 설정하면 원탭 실행 가능

**Step 3: 결과 알림 (선택)**
```
액션 추가: 알림 보내기
제목: "영수증 접수됨"
본문: [URL 내용 가져오기 결과의 공급처 필드]
```

---

## 4. Google Sheets 구조

[영수증 자동화 관리](https://docs.google.com/spreadsheets/d/12h48_MBGeRmGXP2exEY3fNFAPxYdnnAqZc6T8VbnBp0/edit)

| 날짜 | 공급처 | 공급가액(원) | VAT(원) | 합계금액(원) | 카테고리 | 메모 | Drive URL | Slack 전송시각 | 이상치 |
|------|--------|------------|--------|------------|--------|------|-----------|--------------|------|

---

## 5. Google Drive 구조

```
영수증_자동화/
├── 2026-03/
│   ├── receipt_20260327_143022.jpg
│   └── receipt_20260328_091500.jpg
├── 2026-04/
│   └── ...
```

---

## 6. 주간 이상치 보고 (매주 금요일 18:00 KST)

Slack 개인 DM으로 수신:
```
📊 주간 영수증 리포트 (03/23 ~ 03/27)

이번 주 요약
> 건수: 12건 | 합계: 1,234,500원 | VAT: 112,227원

⚠️ 누락 의심 날짜 (1일)
  • 2026-03-25 — 영수증 없음

🚨 금액 이상치 (1건)
  • 2026-03-26 | ○○식당 | 820,000원 | 고액(820,000원 > 임계500,000원)

📋 Google Sheets 바로가기
```

---

## 7. 이상치 판정 기준

| 유형 | 기준 |
|------|------|
| 고액 | 합계금액 > 주평균×3 또는 50만원 |
| VAT 비율 이상 | VAT/공급가액이 8~12% 범위 벗어남 |
| 누락 의심 | 영업일(월~금) 중 영수증 0건 날짜 |
