"""
Claude T — Sigma Rank 자동 트레이딩 봇
데이터: OKX OnchainOS DEX API + OKX 선물 API
분석: Claude API
실행 주기: 30분
"""

import os
import json
import hmac
import math
from typing import Optional
import hashlib
import base64
import logging
import time
import schedule
from datetime import datetime, timezone
from dotenv import load_dotenv
import requests
import anthropic

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log"),
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("ClaudeT")

# ── 환경변수 ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
OKX_API_KEY          = os.environ["OKX_API_KEY"]
OKX_SECRET_KEY       = os.environ["OKX_SECRET_KEY"]
OKX_PASSPHRASE       = os.environ["OKX_PASSPHRASE"]           # 선물 API용
OKX_WEB3_API_KEY     = os.environ.get("OKX_WEB3_API_KEY", OKX_API_KEY)
OKX_WEB3_SECRET_KEY  = os.environ.get("OKX_WEB3_SECRET_KEY", OKX_SECRET_KEY)
OKX_WEB3_PASSPHRASE  = os.environ.get("OKX_WEB3_PASSPHRASE", OKX_PASSPHRASE)  # Web3 DEX API용
TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID    = os.environ["TELEGRAM_CHAT_ID"]

# ── 클라이언트 초기화 ─────────────────────────────────────────────────────────
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── OKX 계약 단위 (1계약당 기초자산 수량) ──────────────────────────────────────
CONTRACT_SIZE = {
    "BTC-USDT-SWAP": 0.01,   # 1계약 = 0.01 BTC
    "ETH-USDT-SWAP": 0.1,    # 1계약 = 0.1 ETH
}
INST_ID = {
    "BTC": "BTC-USDT-SWAP",
    "ETH": "ETH-USDT-SWAP",
}

# ── OKX OnchainOS DEX API 설정 ────────────────────────────────────────────────
ONCHAIN_BASE = "https://web3.okx.com/api/v6/dex"

# ETH 체인 인덱스 (BTC 네이티브 체인은 DEX 시그널 미지원, WBTC/ETH 기준 활용)
CHAIN_ETH = "1"   # Ethereum mainnet
CHAIN_BTC = "0"   # Bitcoin mainnet (인덱스 가격 조회용)

# 네이티브 토큰은 빈 문자열
ETH_CONTRACT  = ""
BTC_CONTRACT  = ""

# ETH 체인 위의 WBTC (고래 시그널용 — BTC 대리 지표)
WBTC_CONTRACT = "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"
# WETH (ETH 고래 시그널용 — 네이티브 ETH는 contract 없음)
WETH_CONTRACT = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"

# ── OKX 선물 API 설정 ─────────────────────────────────────────────────────────
FUTURES_BASE = "https://www.okx.com"

# ── 초기 잔액 (봇 시작 시 1회 조회) ──────────────────────────────────────────
INITIAL_BALANCE: float = 0.0

# ── OI 직전 사이클 메모리 (변화율 계산용) ──────────────────────────────────────
OI_PREV: dict = {}           # {inst_id: float} — 직전 사이클 OI 계약수
PREV_OPEN_INST_IDS: set = set()  # 직전 사이클에 열려 있던 inst_id 집합 (TP/SL 체결 감지용)



def send_telegram(message: str):
    """텔레그램으로 알림 전송"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
        log.info("[Telegram] 알림 전송 완료")
    except Exception as e:
        log.warning(f"[Telegram] 알림 전송 실패: {e}")


def _okx_timestamp() -> str:
    """현재 UTC 타임스탬프를 단일 호출로 생성 (race condition 방지)"""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + str(now.microsecond // 1000).zfill(3) + "Z"


def _okx_sign(secret: str, timestamp: str, method: str, path: str, body: str = "") -> str:
    """OKX API HMAC-SHA256 서명 생성"""
    message = timestamp + method + path + body
    mac = hmac.new(secret.encode(), message.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


def _web3_headers(method: str, path: str, body: str = "") -> dict:
    """OnchainOS DEX API 헤더 (Web3 전용 키 사용)"""
    ts = _okx_timestamp()
    return {
        "OK-ACCESS-KEY":       OKX_WEB3_API_KEY,
        "OK-ACCESS-SIGN":      _okx_sign(OKX_WEB3_SECRET_KEY, ts, method, path, body),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE":OKX_WEB3_PASSPHRASE,
        "Content-Type":        "application/json",
    }


def _futures_headers(method: str, path: str, body: str = "") -> dict:
    """OKX 선물 API 헤더 (일반 키 사용)"""
    ts = _okx_timestamp()
    return {
        "OK-ACCESS-KEY":       OKX_API_KEY,
        "OK-ACCESS-SIGN":      _okx_sign(OKX_SECRET_KEY, ts, method, path, body),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE":OKX_PASSPHRASE,
        "Content-Type":        "application/json",
    }


# ── OnchainOS DEX 데이터 수집 ─────────────────────────────────────────────────

def fetch_onchain_price(chain_index: str, contract: str, label: str) -> dict:
    """온체인 인덱스 현재가 조회"""
    path = "/api/v6/dex/index/current-price"
    url = ONCHAIN_BASE + "/index/current-price"
    payload = [{"chainIndex": chain_index, "tokenContractAddress": contract}]
    body = json.dumps(payload)
    try:
        r = requests.post(url, data=body, headers=_web3_headers("POST", path, body), timeout=10)
        data = r.json()
        if data.get("code") == "0" and data.get("data"):
            price = data["data"][0].get("price", "N/A")
            log.info(f"[OnChain] {label} 가격: {price}")
            return {"price": price}
    except Exception as e:
        log.warning(f"[OnChain] {label} 가격 조회 실패: {e}")
    return {"price": None}


def fetch_whale_signals(chain_index: str, token_address: str, label: str) -> list:
    """고래/스마트머니 시그널 조회 (walletType: 1=스마트머니, 3=고래)"""
    path = "/api/v6/dex/market/signal/list"
    url = ONCHAIN_BASE + "/market/signal/list"
    payload = {
        "chainIndex":   chain_index,
        "walletType":   "1,3",
        "tokenAddress": token_address,
        "limit":        "20",
    }
    body = json.dumps(payload)
    try:
        r = requests.post(url, data=body, headers=_web3_headers("POST", path, body), timeout=10)
        data = r.json()
        if data.get("code") == "0":
            signals = data.get("data", [])
            log.info(f"[OnChain] {label} 고래 시그널: {len(signals)}건")
            return signals
    except Exception as e:
        log.warning(f"[OnChain] {label} 고래 시그널 실패: {e}")
    return []


def fetch_holder_overview(chain_index: str, contract: str, label: str) -> dict:
    """토큰 홀더 집중도 분석"""
    if not contract:
        return {}  # 네이티브 토큰(BTC chain)은 미지원
    qs = f"?chainIndex={chain_index}&tokenContractAddress={contract}"
    path = "/api/v6/dex/market/token/cluster/overview" + qs
    url = ONCHAIN_BASE + "/market/token/cluster/overview"
    params = {"chainIndex": chain_index, "tokenContractAddress": contract}
    try:
        r = requests.get(url, params=params, headers=_web3_headers("GET", path), timeout=10)
        data = r.json()
        if data.get("code") == "0":
            overview = data.get("data", {})
            log.info(f"[OnChain] {label} 홀더 집중도: {overview.get('ClusterConcentration', 'N/A')}")
            return overview
    except Exception as e:
        log.warning(f"[OnChain] {label} 홀더 분석 실패: {e}")
    return {}


# ── OKX 선물 API 데이터 수집 ──────────────────────────────────────────────────
# 미결제약정, 펀딩비, 롱숏 비율은 공개 엔드포인트 (인증 불필요)

def fetch_open_interest(inst_id: str) -> dict:
    """미결제약정 조회 (예: BTC-USDT-SWAP)"""
    path = f"/api/v5/public/open-interest?instType=SWAP&instId={inst_id}"
    try:
        r = requests.get(FUTURES_BASE + path, timeout=10)
        data = r.json()
        if data.get("code") == "0" and data.get("data"):
            oi = data["data"][0]
            log.info(f"[Futures] {inst_id} OI: {oi.get('oi')} 계약 / {oi.get('oiCcy')} 코인")
            return oi
    except Exception as e:
        log.warning(f"[Futures] {inst_id} 미결제약정 조회 실패: {e}")
    return {}


def fetch_funding_rate(inst_id: str) -> dict:
    """현재 펀딩비 조회"""
    path = f"/api/v5/public/funding-rate?instId={inst_id}"
    try:
        r = requests.get(FUTURES_BASE + path, timeout=10)
        data = r.json()
        if data.get("code") == "0" and data.get("data"):
            fr = data["data"][0]
            log.info(f"[Futures] {inst_id} 펀딩비: {fr.get('fundingRate')}")
            return fr
    except Exception as e:
        log.warning(f"[Futures] {inst_id} 펀딩비 조회 실패: {e}")
    return {}


def fetch_long_short_ratio(inst_id: str) -> dict:
    """롱/숏 계정 비율 조회 (인증 필요)"""
    ccy = inst_id.split("-")[0]  # "BTC-USDT-SWAP" → "BTC"
    path = f"/api/v5/rubik/stat/contracts/long-short-account-ratio?instId={inst_id}&period=5m&ccy={ccy}"
    try:
        r = requests.get(FUTURES_BASE + path, headers=_futures_headers("GET", path), timeout=10)
        data = r.json()
        if data.get("code") == "0" and data.get("data"):
            latest = data["data"][0]  # 가장 최근
            log.info(f"[Futures] {inst_id} 롱숏비율: {latest}")
            return latest
    except Exception as e:
        log.warning(f"[Futures] {inst_id} 롱숏비율 조회 실패: {e}")
    return {}




def fetch_premium(inst_id: str) -> dict:
    """선물 프리미엄 = (mark price - index price) / index price × 100
    양수 = 선물이 현물 위 (강세), 음수 = 선물이 현물 아래 (약세)"""
    mark_path  = f"/api/v5/public/mark-price?instType=SWAP&instId={inst_id}"
    index_id   = inst_id.replace("-USDT-SWAP", "-USDT")
    index_path = f"/api/v5/public/index-tickers?instId={index_id}"
    try:
        r_mark  = requests.get(FUTURES_BASE + mark_path,  timeout=10)
        r_index = requests.get(FUTURES_BASE + index_path, timeout=10)
        d_mark  = r_mark.json()
        d_index = r_index.json()
        if (d_mark.get("code") == "0" and d_mark.get("data") and
                d_index.get("code") == "0" and d_index.get("data")):
            mark_px  = float(d_mark["data"][0]["markPx"])
            index_px = float(d_index["data"][0]["idxPx"])
            premium  = (mark_px - index_px) / index_px * 100
            log.info(f"[Premium] {inst_id} 프리미엄: {premium:+.4f}%")
            return {"mark_px": mark_px, "index_px": index_px, "premium_pct": round(premium, 4)}
    except Exception as e:
        log.warning(f"[Premium] {inst_id} 프리미엄 조회 실패: {e}")
    return {}


# ── 전체 데이터 수집 ──────────────────────────────────────────────────────────

def collect_market_data() -> dict:
    """BTC/ETH 온체인 + 선물 데이터 수집 통합"""
    log.info("=== 시장 데이터 수집 시작 ===")
    data = {
        "ETH": {
            "onchain_price":    fetch_onchain_price(CHAIN_ETH, ETH_CONTRACT, "ETH"),
            "whale_signals":    fetch_whale_signals(CHAIN_ETH, WETH_CONTRACT, "ETH(WETH)"),
            "holder_overview":  fetch_holder_overview(CHAIN_ETH, WETH_CONTRACT, "ETH(WETH)"),
            "open_interest":    fetch_open_interest("ETH-USDT-SWAP"),
            "funding_rate":     fetch_funding_rate("ETH-USDT-SWAP"),
            "long_short_ratio": fetch_long_short_ratio("ETH-USDT-SWAP"),
            "premium":          fetch_premium("ETH-USDT-SWAP"),
        },
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }
    log.info("=== 시장 데이터 수집 완료 ===")
    return data


# ── Claude 분석 ───────────────────────────────────────────────────────────────

def analyze_with_claude(market_data: dict, current_positions: list) -> dict:
    """Claude API로 시장 데이터 분석 → 트레이딩 판단"""
    open_pos_summary = []
    for p in current_positions:
        open_pos_summary.append({
            "id":          p["id"],
            "asset":       p["asset"],
            "direction":   p["direction"],
            "entry_price": p["entry_price"],
            "tp":          p["tp"],
            "sl":          p["sl"],
        })

    prompt = f"""
You are Claude T, an AI trader participating in Sigma Rank — a paper trading competition.
Analyze the following market data and return a single JSON decision.

## Current Open Positions (Claude T)
{json.dumps(open_pos_summary, indent=2)}

## Market Data (collected at {market_data['collected_at']})
{json.dumps(market_data, indent=2, default=str)}

## Rules
- You only trade ETH. Always set "asset" to "ETH".
- You can only hold ONE position at a time.
- Exits are handled automatically by TP/SL orders on OKX. Do NOT output "close".
- Confidence must reflect genuine signal strength. If signals are mixed or weak, use "hold".
- confidence below 0.65 must always result in "hold", no exceptions.
- **You must evaluate LONG and SHORT opportunities with equal weight. Do NOT default to short.**

## Data Fields (use these in your analysis)
- `premium.premium_pct`: (futures mark price - spot index) / spot × 100
  - Positive premium = futures trading ABOVE spot → bullish demand in derivatives
  - Negative premium = futures trading BELOW spot → bearish sentiment

## LONG Entry Triggers (enter long when 2+ on-chain signals align)
- **Crowded shorts / funding flip**: fundingRate ≤ 0 or close to zero (shorts over-positioned → rebound likely)
- **Whale accumulation**: clear buy-side flow detected for WETH (smart money accumulating)
- **Extreme short positioning**: L/S ratio ≤ 0.6 (shorts at extreme → contrarian long)
- **OI surge + low funding**: oi_change_pct ≥ +2% AND fundingRate ≤ +0.005% (new long positions entering without longs overpaying)

## SHORT Entry Triggers (enter short when 2+ signals align)
- **Reversal short**: L/S ratio ≥ 1.8 AND oi_change_pct ≤ 0 AND premium_pct < -0.02%
  → Longs at extreme AND OI declining AND futures below spot = long unwind in progress
- **Funding spike short**: fundingRate ≥ +0.02% AND premium_pct < -0.02%
  → Longs paying heavily but price not following through = overextended
- **Whale distribution**: clear sell-side flow detected for WETH

## CRITICAL: L/S ratio 1.2–1.7 is NORMAL in a bull market — do NOT treat it as a short signal alone.
##           If you have only L/S ratio as your short reason, that is NOT enough — hold instead.

## Response Format
Return ONLY valid JSON, no other text:
{{
  "action": "long" | "short" | "hold",
  "asset": "ETH",
  "confidence": <float 0.0–1.0>,
  "reason_open": "<why enter now — required for long/short. Be specific: cite which signals triggered this (e.g. momentum +3.2% 2h, OI +2.1%, premium +0.05% → long). 2-3 sentences.>"
}}

If action is "hold", confidence can be null and reason_open can be empty string.
"""

    system_prompt = (
        "너는 온체인 + 파생상품 데이터 기반 이벤트 드리븐 스윙 트레이더야. "
        "평소엔 신중하게 홀드하고, 명확한 복합 신호가 생길 때만 진입해. "
        "롱과 숏 두 방향을 동등하게 분석해야 해 — 숏 편향 절대 금지.\n\n"
        "핵심 판단 원칙:\n"
        "1. 펀딩비가 마이너스이거나 0에 가까우면 숏 포지션 과다 — 롱 반등 기회야.\n"
        "2. 고래 매집 시그널은 강력한 롱 진입 트리거야. 적극 반영해.\n"
        "3. 롱숏비율 0.6 이하는 숏이 극단적으로 쏠린 것 — 역추세 롱 기회.\n"
        "4. OI 급증 + 펀딩비 낮음은 새 롱 포지션 유입 신호 — 롱 우호적.\n"
        "5. 롱숏비율 1.2~1.7은 강세장 정상 범위야. 이것만으로 숏 진입하지 마.\n"
        "6. 프리미엄(선물-현물 차이)이 양수면 선물 매수세 강한 것, 롱 우호적 신호야.\n\n"
        "confidence 0.65 미만이면 무조건 hold. 동시에 하나의 포지션만 유지. "
        "수익률 최우선, 트레이딩 빈도는 하루 1~2회면 충분. "
        "매크로 이벤트가 데이터에 반영된 신호로 보이면 적극 반영해."
    )

    log.info("[Claude] 분석 요청 중...")
    response = claude.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    log.info(f"[Claude] 응답: {raw}")

    # JSON 파싱
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    decision = json.loads(raw.strip())
    return decision


# ── OKX 계좌 / 트레이딩 액션 ─────────────────────────────────────────────────

def get_bot_wallet_balance() -> float:
    """OKX 계좌 USDT 가용 잔액 조회"""
    path = "/api/v5/account/balance?ccy=USDT"
    try:
        r = requests.get(
            FUTURES_BASE + path,
            headers=_futures_headers("GET", path),
            timeout=10,
        )
        data = r.json()
        if data.get("code") == "0" and data.get("data"):
            details = data["data"][0].get("details", [])
            for d in details:
                if d.get("ccy") == "USDT":
                    balance = float(d.get("availBal", 0))
                    log.info(f"[Account] USDT 가용잔액: {balance}")
                    return balance
    except Exception as e:
        log.warning(f"[Account] 잔액 조회 실패: {e}")
    return 0.0


def get_open_positions() -> list:
    """OKX 무기한 선물 열린 포지션 조회 (BTC/ETH USDT-마진 SWAP)"""
    path = "/api/v5/account/positions?instType=SWAP"
    try:
        r = requests.get(
            FUTURES_BASE + path,
            headers=_futures_headers("GET", path),
            timeout=10,
        )
        data = r.json()
        if data.get("code") == "0":
            positions = []
            for p in data.get("data", []):
                inst_id = p.get("instId", "")
                # BTC-USDT-SWAP, ETH-USDT-SWAP만 필터
                if inst_id not in CONTRACT_SIZE:
                    continue
                pos_size = float(p.get("pos", 0))
                if pos_size == 0:
                    continue
                asset_base = inst_id.split("-")[0]  # "BTC" or "ETH"
                # One-way 모드: pos 양수 = long, 음수 = short
                direction = "long" if pos_size > 0 else "short"
                positions.append({
                    "id":          inst_id,                          # 청산 시 instId로 활용
                    "asset":       asset_base + "USDT",             # "BTCUSDT" 형식 (run_bot 매칭용)
                    "inst_id":     inst_id,
                    "direction":   direction,
                    "entry_price": float(p.get("avgPx") or 0),
                    "pos":         abs(pos_size),
                    "upl":         float(p.get("upl") or 0),
                    "mgn_mode":    p.get("mgnMode", "cross"),
                    "tp":          None,
                    "sl":          None,
                })
            log.info(f"[Account] 열린 포지션: {len(positions)}개")
            return positions
    except Exception as e:
        log.warning(f"[Account] 포지션 조회 실패: {e}")
    return []


def resolve_leverage(wallet: float, confidence: float, entry_price: float,
                     inst_id: str) -> tuple[int, str]:
    """
    2-프레임 레버리지 결정 로직.

    1단계 (상대 프레임): 잔액 대비 초기 잔액 비율로 base_leverage 산출
    2단계 (절대 프레임): 1계약 진입에 필요한 최소 명목가치 미달 시 레버리지 상향
    3단계: 5배에서도 계약 수 < 1이면 None 반환 (스킵 신호)

    Returns:
        (final_leverage, "ok") — 정상 진입 가능
        (0,              "skip:<message>") — 진입 불가
    """
    # 1단계 — 상대 프레임
    base_leverage = min(5, max(1, round(INITIAL_BALANCE / wallet))) if wallet > 0 else 1

    ct_val       = CONTRACT_SIZE[inst_id]
    min_notional = entry_price * ct_val          # 1계약 진입에 필요한 최소 명목가치

    # 2단계 — 절대 프레임: 현재 레버리지로 최소 1계약 치 명목가치 확보 가능한지 확인
    notional = wallet * confidence * base_leverage
    if notional < min_notional:
        required = math.ceil(min_notional / (wallet * confidence)) if wallet * confidence > 0 else 6
        final_leverage = min(5, required)
    else:
        final_leverage = base_leverage

    # 3단계 — 5배에서도 불가능한지 최종 확인
    max_notional = wallet * confidence * 5
    if max_notional < min_notional:
        min_wallet_needed = min_notional / (confidence * 5)
        msg = (
            f"최소 거래 불가 — {inst_id} 1계약 진입에 필요한 최소 잔액: "
            f"{min_wallet_needed:.2f} USDT (레버리지 5배 기준)"
        )
        return 0, f"skip:{msg}"

    return final_leverage, "ok"


def calc_contracts(wallet_balance: float, confidence: float, leverage: int,
                   entry_price: float, inst_id: str) -> int:
    """OKX 계약 수 계산 (정수 단위)"""
    margin    = wallet_balance * confidence      # 마진 = 잔액 × confidence
    notional  = margin * leverage                # 명목가치 = 마진 × 레버리지
    ct_val    = CONTRACT_SIZE[inst_id]           # 1계약당 코인 수
    contracts = int(notional / (entry_price * ct_val))
    return contracts


def place_tp_sl(inst_id: str, action: str, entry_price: float,
                confidence: float, contracts: int) -> None:
    """OCO 알고 오더로 TP/SL 설정 (One-way 모드, posSide 없음)"""
    if action == "long":
        tp_price   = entry_price * (1 + confidence * 0.06)
        sl_price   = entry_price * (1 - ((1 - confidence) * 0.05 + 0.01))
        close_side = "sell"
    else:  # short
        tp_price   = entry_price * (1 - confidence * 0.06)
        sl_price   = entry_price * (1 + (1 - confidence) * 0.05 + 0.01)
        close_side = "buy"

    path = "/api/v5/trade/order-algo"
    body_dict = {
        "instId":      inst_id,
        "tdMode":      "cross",
        "side":        close_side,
        "ordType":     "oco",
        "sz":          str(contracts),
        "tpTriggerPx": f"{tp_price:.2f}",
        "tpOrdPx":     "-1",       # 시장가 TP
        "slTriggerPx": f"{sl_price:.2f}",
        "slOrdPx":     "-1",       # 시장가 SL
        "reduceOnly":  "true",     # 포지션 없을 때 역방향 신규 오픈 방지
    }
    body = json.dumps(body_dict)

    log.info(f"[Bot] TP/SL 설정: {action.upper()} TP={tp_price:.2f} SL={sl_price:.2f}")

    try:
        r = requests.post(
            FUTURES_BASE + path,
            data=body,
            headers=_futures_headers("POST", path, body),
            timeout=10,
        )
        data = r.json()
        if data.get("code") == "0" and data.get("data"):
            algo_id = data["data"][0].get("algoId", "")
            log.info(f"[Bot] TP/SL 설정 완료: algoId={algo_id}")
        else:
            log.error(f"[Bot] TP/SL 설정 실패: {data}")
    except Exception as e:
        log.error(f"[Bot] TP/SL 설정 예외: {e}")


def open_position(decision: dict, entry_price: float) -> Optional[str]:
    """OKX /api/v5/trade/order — USDT 마진 무기한 선물 시장가 주문 (One-way 모드)"""
    wallet = get_bot_wallet_balance()
    confidence = float(decision["confidence"])

    asset   = decision["asset"]          # "BTC" or "ETH"
    inst_id = INST_ID[asset]

    # 2-프레임 레버리지 결정
    leverage, status = resolve_leverage(wallet, confidence, entry_price, inst_id)
    if status.startswith("skip:"):
        log.warning(f"[Bot] {status[5:]}")
        return None

    contracts = calc_contracts(wallet, confidence, leverage, entry_price, inst_id)

    if contracts <= 0:
        log.warning("[Bot] 계약 수가 0 이하 — 포지션 건너뜀")
        return None

    action = decision["action"]        # "long" or "short"
    side = "buy" if action == "long" else "sell"

    path = "/api/v5/trade/order"
    body_dict = {
        "instId":  inst_id,
        "tdMode":  "cross",            # 교차마진
        "side":    side,               # One-way 모드: long→buy, short→sell
        "ordType": "market",
        "sz":      str(contracts),
    }
    body = json.dumps(body_dict)

    log.info(
        f"[Bot] 주문 전송: {inst_id} {action.upper()} "
        f"contracts={contracts} lev={leverage}x entry≈{entry_price}"
    )

    try:
        r = requests.post(
            FUTURES_BASE + path,
            data=body,
            headers=_futures_headers("POST", path, body),
            timeout=10,
        )
        data = r.json()
        if data.get("code") == "0" and data.get("data"):
            ord_id = data["data"][0].get("ordId", "")
            log.info(f"[Bot] 주문 완료: ordId={ord_id}")
            # confidence 기반 동적 TP/SL OCO 알고 오더
            place_tp_sl(inst_id, action, entry_price, confidence, contracts)
            return inst_id  # 후속 처리에서 instId를 포지션 식별자로 사용
        else:
            log.error(f"[Bot] 주문 실패: {data}")
    except Exception as e:
        log.error(f"[Bot] 주문 예외: {e}")
    return None


def cancel_algo_orders(inst_id: str) -> None:
    """해당 instId의 활성 OCO 알고 오더 전체 취소 (수동 청산 후 잔여 오더 제거)"""
    path = f"/api/v5/trade/orders-algo-pending?ordType=oco&instId={inst_id}"
    try:
        r = requests.get(
            FUTURES_BASE + path,
            headers=_futures_headers("GET", path),
            timeout=10,
        )
        data = r.json()
        if data.get("code") != "0":
            log.warning(f"[Bot] 알고 오더 조회 실패: {data}")
            return
        orders = data.get("data", [])
        if not orders:
            log.info(f"[Bot] {inst_id} 취소할 알고 오더 없음")
            return
        algo_ids = [{"algoId": o["algoId"], "instId": inst_id} for o in orders]
        cancel_path = "/api/v5/trade/cancel-algos"
        cancel_body = json.dumps(algo_ids)
        r2 = requests.post(
            FUTURES_BASE + cancel_path,
            data=cancel_body,
            headers=_futures_headers("POST", cancel_path, cancel_body),
            timeout=10,
        )
        result = r2.json()
        if result.get("code") == "0":
            log.info(f"[Bot] 알고 오더 {len(algo_ids)}건 취소 완료: {inst_id}")
        else:
            log.error(f"[Bot] 알고 오더 취소 실패: {result}")
    except Exception as e:
        log.error(f"[Bot] 알고 오더 취소 예외: {e}")


def close_position(inst_id: str, exit_price: float) -> bool:
    """OKX /api/v5/trade/close-position — 전체 포지션 시장가 청산"""
    log.info(f"[Bot] 포지션 청산: {inst_id} @ ≈{exit_price}")
    path = "/api/v5/trade/close-position"
    body_dict = {
        "instId":  inst_id,
        "mgnMode": "cross",
    }
    body = json.dumps(body_dict)

    try:
        r = requests.post(
            FUTURES_BASE + path,
            data=body,
            headers=_futures_headers("POST", path, body),
            timeout=10,
        )
        data = r.json()
        if data.get("code") == "0":
            log.info(f"[Bot] 청산 완료: {inst_id}")
            # 수동 청산 후 잔여 OCO 알고 오더 취소 (재진입 시 충돌 방지)
            cancel_algo_orders(inst_id)
            return True
        else:
            log.error(f"[Bot] 청산 실패: {data}")
    except Exception as e:
        log.error(f"[Bot] 청산 예외: {e}")
    return False


def get_current_price_from_onchain(asset: str, market_data: dict) -> Optional[float]:
    """수집된 온체인 가격에서 현재가 추출"""
    price_raw = market_data.get(asset, {}).get("onchain_price", {}).get("price")
    return float(price_raw) if price_raw else None


def fetch_close_price(inst_id: str) -> str:
    """포지션 히스토리에서 가장 최근 청산가(closeAvgPx) 조회"""
    path = f"/api/v5/account/positions-history?instType=SWAP&instId={inst_id}&limit=1"
    try:
        r = requests.get(
            FUTURES_BASE + path,
            headers=_futures_headers("GET", path),
            timeout=10,
        )
        data = r.json()
        if data.get("code") == "0" and data.get("data"):
            close_px = data["data"][0].get("closeAvgPx")
            if close_px:
                return f"${float(close_px):,.2f}"
    except Exception as e:
        log.warning(f"[Bot] 청산가 조회 실패: {e}")
    return "청산가 확인 불가"


def translate_to_korean(text: str) -> str:
    """Claude API로 텍스트를 한국어로 번역 (실패 시 원본 반환)"""
    if not text:
        return text
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": (
                    f"다음 텍스트를 자연스러운 한국어로 번역해줘. "
                    f"번역문만 출력하고 다른 말은 하지 마:\n\n{text}"
                ),
            }],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        log.warning(f"[번역] 실패, 원본 사용: {e}")
        return text


# ── 메인 봇 루프 ──────────────────────────────────────────────────────────────

def run_bot():
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("Claude T 봇 실행 시작")

    # 1. 시장 데이터 수집
    market_data = collect_market_data()

    # OI 변화율 계산 및 주입 (직전 사이클 대비)
    global OI_PREV
    for asset in ("ETH",):
        inst_id = INST_ID[asset]
        oi_now = float(market_data[asset].get("open_interest", {}).get("oi") or 0)
        if inst_id in OI_PREV and OI_PREV[inst_id] > 0:
            oi_change_pct = (oi_now - OI_PREV[inst_id]) / OI_PREV[inst_id] * 100
            market_data[asset]["oi_change_pct"] = round(oi_change_pct, 3)
            log.info(f"[Bot] {inst_id} OI 변화율: {oi_change_pct:+.3f}%")
        else:
            market_data[asset]["oi_change_pct"] = None
            log.info(f"[Bot] {inst_id} OI 변화율: 첫 사이클 (기준값 없음)")
        if oi_now > 0:
            OI_PREV[inst_id] = oi_now

    # 2. 현재 열린 포지션 조회
    open_positions = get_open_positions()
    log.info(f"[Bot] 열린 포지션: {len(open_positions)}개")

    # TP/SL 자동 체결 감지 — 직전 사이클에 있던 포지션이 사라졌으면 청산된 것
    global PREV_OPEN_INST_IDS
    current_inst_ids = {p["inst_id"] for p in open_positions}
    closed_inst_ids  = PREV_OPEN_INST_IDS - current_inst_ids
    for closed_id in closed_inst_ids:
        asset_name = closed_id.split("-")[0]
        close_px   = fetch_close_price(closed_id)
        log.info(f"[Bot] TP/SL 체결 감지: {closed_id} 포지션 청산됨 (청산가: {close_px})")
        send_telegram(
            f"[{asset_name} 포지션 청산]\n"
            f"청산가: {close_px}\n"
            f"OKX TP/SL 주문이 체결되어 포지션이 자동으로 닫혔습니다."
        )
    PREV_OPEN_INST_IDS = current_inst_ids


    # 3. Claude 분석
    try:
        decision = analyze_with_claude(market_data, open_positions)
    except (json.JSONDecodeError, KeyError) as e:
        log.error(f"[Claude] 응답 파싱 실패: {e}")
        return

    action = decision.get("action", "hold")
    asset  = decision.get("asset", "ETH")
    log.info(f"[Claude] 판단: {action.upper()} {asset} (confidence={decision.get('confidence')})")

    if action == "hold":
        log.info("[Bot] hold — 이번 사이클 패스")
        return

    # asset 유효성 검사
    if asset not in INST_ID:
        log.error(f"[Bot] 알 수 없는 asset: {asset} — 중단")
        return

    # confidence None/부족 체크 (long/short 진입 시)
    if action in ("long", "short"):
        confidence = decision.get("confidence")
        if confidence is None:
            log.warning("[Bot] confidence 없음 — 진입 건너뜀")
            return
        if float(confidence) < 0.65:
            log.warning(f"[Bot] confidence {confidence} < 0.65 — 진입 건너뜀")
            return

    # 현재가 (온체인 기준)
    current_price = get_current_price_from_onchain(asset, market_data)
    if not current_price:
        log.error(f"[Bot] {asset} 현재가 없음 — 중단")
        return

    # 4. 포지션 열기 (long/short)
    if action in ("long", "short"):
        # 같은 자산 기존 포지션 있으면 건너뜀
        existing = [p for p in open_positions if p["asset"] == asset + "USDT"]
        if existing:
            log.info(f"[Bot] {asset} 포지션 이미 존재 — 이번 사이클 오픈 건너뜀")
            return

        pos_id = open_position(decision, current_price)
        if pos_id:
            direction_kor = "롱 (상승 베팅)" if action == "long" else "숏 (하락 베팅)"
            conf_pct = int(float(decision.get('confidence', 0)) * 100)
            reason_kor = translate_to_korean(decision.get('reason_open', ''))
            send_telegram(
                f"[ETH {direction_kor}] 포지션 진입\n"
                f"진입가: ${current_price:,.2f}\n"
                f"신호 강도: {conf_pct}%\n"
                f"이유: {reason_kor}"
            )

    log.info("Claude T 봇 실행 완료")
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")


# ── 스케줄러 ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("Claude T 봇 스케줄러 시작 (30분 주기)")
    # 초기 잔액 1회 조회 → 레버리지 계산 기준값으로 사용
    INITIAL_BALANCE = get_bot_wallet_balance()
    if INITIAL_BALANCE <= 0:
        log.error("[Bot] 초기 잔액 조회 실패 — 봇 종료")
        raise SystemExit(1)
    log.info(f"[Bot] 초기 잔액: {INITIAL_BALANCE:.2f} USDT (레버리지 기준)")
    run_bot()  # 시작 즉시 1회 실행
    schedule.every(30).minutes.do(run_bot)
    while True:
        schedule.run_pending()
        time.sleep(30)
