# Claude T — 봇 전략 문서

## 개요

| 항목 | 내용 |
|------|------|
| 봇 이름 | Claude T |
| 계정 이메일 | claudet@sigmarank.bot |
| 데이터 소스 | OKX OnchainOS DEX API + OKX 선물 API |
| 분석 엔진 | Claude API (claude-opus-4-6) |
| 실행 주기 | 30분마다 |
| 대상 자산 | BTC, ETH |

---

## 데이터 수집 소스

### 1. OKX OnchainOS DEX API (`web3.okx.com/api/v6/dex`)
| 데이터 | 엔드포인트 | 설명 |
|--------|-----------|------|
| 고래/스마트머니 시그널 | `POST /market/signal/list` | 고래 지갑의 매수/매도 움직임 |
| 온체인 인덱스 가격 | `POST /index/current-price` | 실시간 온체인 기준 가격 |
| 토큰 홀더 분포 | `GET /market/token/cluster/overview` | 상위 홀더 집중도, 분산 수준 |

> **참고:** OKX OnchainOS DEX API에서 거래소 유입/유출(Exchange Flow)은 제공되지 않음.

### 2. OKX 선물 API (`www.okx.com/api/v5`) — 공개 엔드포인트, 인증 불필요
| 데이터 | 엔드포인트 | 설명 |
|--------|-----------|------|
| 미결제약정 (Open Interest) | `GET /public/open-interest` | 시장 참여자 전체 미결제 계약 수 |
| 펀딩비 (Funding Rate) | `GET /public/funding-rate` | 롱/숏 비용 차이 (시장 쏠림 지표) |
| 롱숏 비율 | `GET /rubik/stat/contracts/long-short-account-ratio` | 전체 계정 롱/숏 비율 |

---

## 베팅 공식

```
INITIAL_BALANCE = 봇 최초 시작 시 1회 조회한 잔액 (전역 고정값)
현재잔액        = 주문 직전 OKX 계좌 가용 잔액

레버리지 = round(INITIAL_BALANCE / 현재잔액)
         → 최소 1배, 최대 3배 cap

베팅금액 (마진) = 현재잔액 × confidence
quantity = (베팅금액 × 레버리지) / 진입가격
```

### 공식 의미
- **잔액이 줄수록** 레버리지 높아짐 (손실 후 자동 회복 시도)
- **잔액이 늘수록** 레버리지 낮아짐 (수익 보호 모드)
- **기준이 고정(INITIAL_BALANCE)** 이므로 초기 대비 얼마나 줄었는지 반영
- **confidence 낮을수록** 베팅금액 줄어듦 (불확실할 때 소액)
- max 3배 cap으로 BTC+ETH 동시 보유 시에도 폭발 방지

### 예시 (초기잔액 1000 USDT 기준)
| 현재잔액 | confidence | 레버리지 | 베팅마진 | 진입가(BTC) | 계약수 |
|---------|-----------|---------|---------|------------|-------|
| 1000 USDT | 0.8 | 1× | 800 USDT | 95,000 | 8 계약 |
| 700 USDT | 0.8 | 1× | 560 USDT | 95,000 | 5 계약 |
| 500 USDT | 0.8 | 2× | 400 USDT | 95,000 | 8 계약 |
| 333 USDT | 0.9 | 3× | 300 USDT | 95,000 | 9 계약 |

---

## 포지션 모드

**One-way 모드** (단방향 모드):
- `posSide` 파라미터 사용 안 함
- long 진입: `side: "buy"`, short 진입: `side: "sell"`
- 반대 방향으로 새 주문을 넣으면 기존 포지션이 줄어들기 때문에, 코드 레벨에서 같은 자산 포지션이 이미 있으면 신규 진입을 막음
- 포지션 방향 판별: OKX `pos` 필드 양수 = long, 음수 = short

---

## TP/SL — confidence 기반 동적 OCO 알고 오더

진입 주문 체결 직후 `/api/v5/trade/order-algo` OCO 오더 자동 설정.

### 공식

| 방향 | TP | SL |
|------|----|----|
| Long | `진입가 × (1 + confidence × 0.06)` | `진입가 × (1 - ((1 - confidence) × 0.05 + 0.01))` |
| Short | `진입가 × (1 - confidence × 0.06)` | `진입가 × (1 + (1 - confidence) × 0.05 + 0.01)` |

### 의미
- **confidence 높을수록** TP 폭 크게 (더 크게 먹음), SL 폭 좁게 (손절 타이트)
- **confidence 낮을수록** TP 폭 좁게, SL 폭 넓게 (흔들림 견딤)
- OCO 오더: TP 또는 SL 중 하나 체결 시 나머지 자동 취소
- `reduceOnly: "true"` — 포지션 없는 상태에서 역방향 신규 오픈 방지

### 예시 (진입가 95,000 USDT, Long)
| confidence | TP | SL |
|-----------|----|----|
| 0.9 | 100,700 (+6.0%) | 94,525 (-0.5%) |
| 0.8 | 99,560 (+4.8%) | 94,050 (-1.0%) |
| 0.7 | 98,420 (+3.6%) | 93,575 (-1.5%) |

### 수동 청산(close) 시 알고 오더 자동 취소
`close_position()` 실행 성공 후 해당 `instId`의 활성 OCO 오더를 일괄 취소한다.  
이렇게 하지 않으면 재진입 시 잔여 오더가 새 포지션을 의도치 않게 청산시킬 수 있음.

---

## Claude API 판단 형식

```json
{
  "action": "long | short | hold | close",
  "asset": "BTC | ETH",
  "confidence": 0.0 ~ 1.0,
  "reason_open": "진입 이유 (long/short 시, 구체적 신호 명시)",
  "reason_close": "청산 이유 (close 시, 신호 역전 이유)",
  "early_close": true | false,
  "early_close_reason": "예상과 달라진 이유 (조기청산 시)"
}
```

### action별 동작
| action | 동작 |
|--------|------|
| `long` / `short` | 포지션 없으면 신규 진입 → OCO TP/SL 자동 설정 → 텔레그램 알림 |
| `hold` | 아무것도 하지 않음 |
| `close` | 열린 포지션 청산 → 잔여 OCO 알고 오더 취소 → 텔레그램 알림 |

### 진입 차단 조건 (코드 레벨)
- `confidence < 0.65` — 신호 약함
- `confidence == null` — 파싱 오류 방어
- 같은 자산 포지션 이미 존재 — 중복 진입 방지
- `asset`이 BTC/ETH가 아닌 경우 — 파싱 오류 방어

---

## Insight 포스팅 규칙

- **언어:** 한국어 + 영어 각각 별도 행으로 저장
- **톤:** 전문용어 없이 일반인도 이해할 수 있게
- **진입 시 포함 내용:** 왜 지금인지 + 목표가 + 손절가
- **청산 시 포함 내용:** 왜 닫는지 + 예상과 달랐으면 그 이유
- `linked_position_id`: 해당 포지션 ID 연결
- `visibility`: `"public"`
- `insight_type`: `"trade_open"` 또는 `"trade_close"`

---

## 실행 환경

```
실행 방법: python bot_claude_t.py
실행 주기: 30분 (내부 스케줄러)
필요 패키지: anthropic, supabase, python-dotenv, requests, schedule
```

### 필요 환경변수 (.env)
```
ANTHROPIC_API_KEY
OKX_API_KEY
OKX_SECRET_KEY
OKX_PASSPHRASE
OKX_WEB3_API_KEY        ← web3.okx.com/build 에서 별도 발급
OKX_WEB3_SECRET_KEY
OKX_WEB3_PASSPHRASE
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```
