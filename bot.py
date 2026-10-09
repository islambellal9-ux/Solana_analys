#!/usr/bin/env python3
"""
Solana Deep Scan & Scalp Bot v2.0 "Jabar Edition"

يفحص أي عنوان (Token Mint Address أو Pair/Pool Address) على سولانا عبر:
  - DexScreener    (المصدر الأساسي: السعر، السيولة، الحجم، البيع/الشراء)
  - GeckoTerminal  (مصدر احتياطي تلقائي للعملات الجديدة جداً اللي ما توصلتش لـ DexScreener بعد)
  - RugCheck       (أمان العقد، توزيع الحاملين، حرق/قفل السيولة)
  - Pump.fun       (مصدر العملة، محفظة المطوّر، حالة الهجرة لـ Raydium)

يحسب تقييماً من 100 (مع عقوبة صريحة إذا السعر فعلاً في موجة هبوط رغم نظافة
العقد)، وإذا كانت العملة قوية (>=70) يولّد خطة سكالبينغ مبنية على Price
Action حقيقي لآخر موجة فقط: يكتشف Swing High/Low من شموع 5 دقائق (أو دقيقة
واحدة كاحتياط)، ويحسب منطقة دخول Golden Zone (تصحيح فيبوناتشي 0.5-0.618)،
وقف خسارة تحت قاع الموجة بـ1.5%، وأهداف بامتدادات فيبوناتشي 1.272/1.618 -
ثم يرسل تقرير عربي منسّق على تيليغرام.

Variables d'environnement requises :
  TELEGRAM_BOT_TOKEN   - توكن البوت من BotFather
  PORT                 - منفذ Health Check (افتراضي 10000، يضبطه Render تلقائياً)

Déploiement Render : Web Service, Start Command -> python bot.py
"""

import asyncio
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional

import aiohttp
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ------------------------------------------------------------------------- #
#  الإعدادات العامة
# ------------------------------------------------------------------------- #

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("solana-scan-bot")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", "10000"))

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=12)
SOLANA_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"
DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search?q={address}"
RUGCHECK_SUMMARY_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
RUGCHECK_FULL_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
PUMPFUN_URL = "https://frontend-api.pump.fun/coins/{mint}"

GECKOTERMINAL_TOKEN_URL = "https://api.geckoterminal.com/api/v2/networks/solana/tokens/{address}"
GECKOTERMINAL_POOLS_URL = "https://api.geckoterminal.com/api/v2/networks/solana/tokens/{address}/pools?page=1"
GECKOTERMINAL_OHLCV_5M_URL = (
    "https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute"
    "?aggregate=5&limit=96&currency=usd"  # ~8 ساعات بشموع 5 دقائق
)
GECKOTERMINAL_OHLCV_1M_URL = (
    "https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute"
    "?aggregate=1&limit=180&currency=usd"  # ~3 ساعات بشموع دقيقة واحدة (احتياطي)
)

NA = "غير متوفر"

# السكالبينغ يُعرض فقط للعملات اللي اجتازت هذا الحد من التقييم
SCALP_MIN_SCORE = 70


# ------------------------------------------------------------------------- #
#  خادم Health Check (مطلوب لـ Render باش يبقى البوت شغال H24)
# ------------------------------------------------------------------------- #

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - اسم الدالة مفروض من http.server
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK - Solana Scan Bot is alive")

    def log_message(self, fmt, *args):  # تعطيل سجلات HTTP المزعجة
        return


def run_health_server(port: int) -> None:
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    log.info("Health check server listening on port %s", port)
    server.serve_forever()


# ------------------------------------------------------------------------- #
#  أدوات مساعدة عامة
# ------------------------------------------------------------------------- #

_MISSING = object()


def safe_get(d: Any, *keys, default=None):
    """يمشي داخل dict/list متداخلة بدون ما يطيح الكود إذا مفتاح ناقص.

    يفرّق بين "المفتاح غير موجود" (يرجّع default) و"المفتاح موجود وقيمته null"
    (يرجّع None فعلاً) - مهم جداً لأن RugCheck يستعمل null بمعنى "الصلاحية ملغاة = آمن".
    """
    cur = d
    for k in keys:
        if cur is None:
            return default
        try:
            cur = cur[k]
        except (KeyError, IndexError, TypeError):
            return default
    return cur


def fmt_usd(n: Optional[float]) -> str:
    if n is None:
        return NA
    try:
        n = float(n)
    except (TypeError, ValueError):
        return NA
    if n >= 1_000_000:
        return f"${n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"${n / 1_000:.1f}K"
    return f"${n:,.2f}"


def fmt_price(n: Optional[float]) -> str:
    if n is None:
        return NA
    try:
        n = float(n)
    except (TypeError, ValueError):
        return NA
    if n == 0:
        return "$0"
    if n >= 1:
        return f"${n:,.4f}"
    # عدد كافٍ من الخانات العشرية للأسعار الصغيرة جداً (شائع في عملات الميم)
    decimals = max(4, -int(f"{n:.1e}".split("e")[1]) + 3)
    return f"${n:.{min(decimals, 14)}f}"


def fmt_pct(n: Optional[float], signed: bool = False) -> str:
    if n is None:
        return NA
    try:
        n = float(n)
    except (TypeError, ValueError):
        return NA
    sign = "+" if (signed and n >= 0) else ""
    return f"{sign}{n:.1f}%"


def to_float(x: Any) -> Optional[float]:
    """تحويل آمن لأي قيمة رقمية قادمة من API خارجي.

    مهم جداً لـ GeckoTerminal تحديداً: يرجّع أغلب حقوله الرقمية كـ *نصوص*
    (مثلاً "45203.12" بدل 45203.12) حسب مواصفة JSON:API اللي يتبعها، وأي
    عملية حسابية أو مقارنة مباشرة على نص كهذا تطيح البوت بخطأ TypeError.
    """
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def age_from_ms(ms: Optional[int]) -> str:
    if not ms:
        return NA
    try:
        seconds = max(0, time.time() - (ms / 1000))
    except (TypeError, ValueError):
        return NA
    if seconds < 3600:
        return f"{int(seconds // 60)} دقيقة"
    if seconds < 86400:
        return f"{int(seconds // 3600)} ساعة"
    return f"{int(seconds // 86400)} يوم"


# ------------------------------------------------------------------------- #
#  طلبات الـ APIs الخارجية (كل واحدة معزولة، فشل وحدة ما يوقفش الباقي)
# ------------------------------------------------------------------------- #

async def fetch_json(session: aiohttp.ClientSession, url: str) -> Optional[dict]:
    try:
        async with session.get(url, timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                log.warning("GET %s -> HTTP %s", url, resp.status)
                return None
            return await resp.json(content_type=None)
    except asyncio.TimeoutError:
        log.warning("Timeout fetching %s", url)
        return None
    except (aiohttp.ClientError, ValueError) as exc:
        log.warning("Error fetching %s: %s", url, exc)
        return None


def _best_liquidity_pair(pairs: list) -> Optional[dict]:
    if not pairs:
        return None
    return max(pairs, key=lambda p: safe_get(p, "liquidity", "usd", default=0) or 0)


async def fetch_dexscreener(session: aiohttp.ClientSession, address: str) -> Optional[dict]:
    """يقبل Token Mint Address أو Pair/Pool Address بلا تفريق.

    يجرّب أولاً مسار /tokens/{address} (الحالة الشائعة: عنوان العملة نفسها).
    إذا رجع فاضي (مثلاً المستخدم أدخل عنوان البركة/الـ Pair مباشرة، أو العملة
    جد جديدة وما تأشّرتش بعد كـ"token"), يجرّب مسار /search?q=address اللي
    يغطي الحالتين.
    """
    data = await fetch_json(session, DEXSCREENER_TOKEN_URL.format(address=address))
    pairs = safe_get(data, "pairs", default=None)
    best = _best_liquidity_pair(pairs)
    if best:
        return best

    data2 = await fetch_json(session, DEXSCREENER_SEARCH_URL.format(address=address))
    pairs2 = safe_get(data2, "pairs", default=None)
    return _best_liquidity_pair(pairs2)


async def fetch_rugcheck(session: aiohttp.ClientSession, mint: str) -> dict:
    """يحاول يجيب التقرير الكامل، وإذا فشل يرجع للملخص. يرجّع dict فاضي عند الفشل الكامل."""
    full = await fetch_json(session, RUGCHECK_FULL_URL.format(mint=mint))
    if full:
        return full
    summary = await fetch_json(session, RUGCHECK_SUMMARY_URL.format(mint=mint))
    return summary or {}


async def fetch_pumpfun(session: aiohttp.ClientSession, mint: str) -> Optional[dict]:
    return await fetch_json(session, PUMPFUN_URL.format(mint=mint))


async def fetch_geckoterminal(session: aiohttp.ClientSession, address: str) -> Optional[dict]:
    """مصدر احتياطي: يُستدعى فقط إذا DexScreener ما رجّعش حاجة (عملة جد جديدة عادة).

    يرجّع dict بنفس "شكل" بركة DexScreener (نفس المفاتيح المستعملة لاحقاً في
    analyze_momentum/analyze_origin) حتى يبقى باقي الكود بلا تغيير.
    """
    token_data = await fetch_json(session, GECKOTERMINAL_TOKEN_URL.format(address=address))
    pools_data = await fetch_json(session, GECKOTERMINAL_POOLS_URL.format(address=address))

    token_attrs = safe_get(token_data, "data", "attributes", default={}) or {}
    pools = safe_get(pools_data, "data", default=[]) or []
    if not token_attrs and not pools:
        return None

    best_pool = None
    if pools:
        best_pool = max(
            pools,
            key=lambda p: float(safe_get(p, "attributes", "reserve_in_usd", default=0) or 0),
        )
    pa = safe_get(best_pool, "attributes", default={}) or {}

    pair_created_ms = None
    created_iso = pa.get("pool_created_at")
    if created_iso:
        try:
            dt = datetime.fromisoformat(str(created_iso).replace("Z", "+00:00"))
            pair_created_ms = dt.timestamp() * 1000
        except ValueError:
            pair_created_ms = None

    pool_address = safe_get(best_pool, "attributes", "address", default=None)

    return {
        "_source_api": "geckoterminal",
        "dexId": "geckoterminal",
        "pairAddress": pool_address,
        "baseToken": {
            "name": token_attrs.get("name"),
            "symbol": token_attrs.get("symbol"),
            "address": address,
        },
        "priceUsd": token_attrs.get("price_usd") or pa.get("base_token_price_usd"),
        "marketCap": token_attrs.get("market_cap_usd"),
        "fdv": token_attrs.get("fdv_usd"),
        "liquidity": {"usd": pa.get("reserve_in_usd")},
        "volume": {
            "m5": safe_get(pa, "volume_usd", "m5", default=None),
            "h1": safe_get(pa, "volume_usd", "h1", default=None) or safe_get(pa, "volume_usd", "h24", default=None),
        },
        "txns": {
            "m5": {
                "buys": safe_get(pa, "transactions", "m5", "buys", default=None),
                "sells": safe_get(pa, "transactions", "m5", "sells", default=None),
            }
        },
        "priceChange": {
            "m5": safe_get(pa, "price_change_percentage", "m5", default=None),
            "h1": safe_get(pa, "price_change_percentage", "h1", default=None),
        },
        "pairCreatedAt": pair_created_ms,
        "url": f"https://www.geckoterminal.com/solana/pools/{pool_address}" if pool_address else None,
    }


# عدد الشموع (قبل القمة) اللي نبحث فيها عن قاع الموجة الأخيرة. نحدّدها
# حتى ما نلقطش قاع قديم من موجة سابقة ما عادش لها علاقة بالحركة الحالية.
SWING_LOOKBACK_CANDLES = 20


def _parse_ohlcv_rows(data: Optional[dict]) -> list:
    """يرجّع شموع [ts, open, high, low, close, volume] مرتبة من الأقدم للأحدث."""
    rows = safe_get(data, "data", "attributes", "ohlcv_list", default=None) or []
    parsed = []
    for row in rows:
        try:
            parsed.append([float(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])])
        except (IndexError, TypeError, ValueError):
            continue
    parsed.sort(key=lambda r: r[0])  # GeckoTerminal يرجّع الأحدث أولاً عادة؛ نرتبها تصاعدياً
    return parsed


def _find_swing_wave(candles: list) -> Optional[dict]:
    """يحدد "الموجة الصاعدة الأخيرة" ضمن نافذة الشموع المعطاة:

    - Swing High = أعلى قمة حالية ضمن النافذة (أحدث/أقوى حركة صاعدة).
    - Swing Low  = أدنى قاع خلال آخر SWING_LOOKBACK_CANDLES شمعة قبل تلك
      القمة مباشرة (بداية الموجة المؤدية لها)، مو قاع الجلسة كاملة.
    """
    if len(candles) < 5:
        return None
    highs = [c[2] for c in candles]
    lows = [c[3] for c in candles]

    high_idx = max(range(len(highs)), key=lambda i: highs[i])
    swing_high = highs[high_idx]

    window_start = max(0, high_idx - SWING_LOOKBACK_CANDLES)
    segment = lows[window_start:high_idx + 1]
    if not segment:
        return None
    swing_low = min(segment)

    if swing_high <= swing_low or swing_low <= 0:
        return None
    return {"swing_low": swing_low, "swing_high": swing_high}


async def fetch_swing_wave(session: aiohttp.ClientSession, pool_address: Optional[str]) -> Optional[dict]:
    """يجيب "الموجة الصاعدة الأخيرة" (قاع البداية + قمة حالية) من شموع 5 دقائق،
    وإذا فشلت (بركة جد جديدة بعدد شموع قليل) يرجع لشموع دقيقة واحدة كاحتياط.

    هذا يستبدل منطق "القاع التاريخي على 48 ساعة" القديم، لأن ذاك القاع ممكن
    يكون بعيد زمنياً وما عادش له علاقة بحركة السعر الحالية - غير مناسب للسكالبينغ.
    """
    if not pool_address:
        return None

    data_5m = await fetch_json(session, GECKOTERMINAL_OHLCV_5M_URL.format(pool=pool_address))
    candles = _parse_ohlcv_rows(data_5m)
    wave = _find_swing_wave(candles)
    if wave:
        wave["timeframe"] = "5m"
        return wave

    data_1m = await fetch_json(session, GECKOTERMINAL_OHLCV_1M_URL.format(pool=pool_address))
    candles_1m = _parse_ohlcv_rows(data_1m)
    wave = _find_swing_wave(candles_1m)
    if wave:
        wave["timeframe"] = "1m"
        return wave

    return None


# ------------------------------------------------------------------------- #
#  التحليل: استخراج كل محور من البيانات الخام
# ------------------------------------------------------------------------- #

def analyze_origin(dex: Optional[dict], pump: Optional[dict]) -> dict:
    source = NA
    if dex and safe_get(dex, "_source_api", default="") == "geckoterminal":
        source = "GeckoTerminal (احتياطي)"
    elif dex:
        dex_id = safe_get(dex, "dexId", default="")
        if dex_id:
            source = {
                "raydium": "Raydium",
                "pumpfun": "Pump.fun",
                "pumpswap": "Pump.fun (PumpSwap)",
                "orca": "Orca",
                "meteora": "Meteora",
            }.get(str(dex_id).lower(), str(dex_id).title())
    if pump is not None and source in (NA, ""):
        source = "Pump.fun" if not safe_get(pump, "complete", default=True) else "Pump.fun → Raydium"
    migrated = None
    if pump is not None:
        migrated = bool(safe_get(pump, "complete", default=False))
    return {"source": source, "migrated": migrated}


def analyze_dev(rug: dict, pump: Optional[dict]) -> dict:
    creator = safe_get(rug, "creator", default=None) or safe_get(rug, "creatorAddress", default=None)
    if not creator and pump is not None:
        creator = safe_get(pump, "creator", default=None)

    top_holders = safe_get(rug, "topHolders", default=None) or safe_get(rug, "holders", default=None) or []
    dev_pct = None
    dev_sold = None
    if creator and isinstance(top_holders, list):
        match = next(
            (h for h in top_holders if str(safe_get(h, "address", default="")).lower() == str(creator).lower()),
            None,
        )
        if match:
            dev_pct = to_float(safe_get(match, "pct", default=None))
            dev_sold = False
        else:
            # المطوّر ظهر كـ creator لكن ما عادش ضمن كبار الحاملين = غالباً باع
            dev_sold = True
            dev_pct = 0.0

    # تاريخ المطوّر (عملات سابقة) - RugCheck أحياناً يعطيه ضمن "risks" أو "creatorTokens"
    prior_rugs = None
    risks = safe_get(rug, "risks", default=[]) or []
    for r in risks:
        name = str(safe_get(r, "name", default="")).lower()
        if "creator" in name or "previous" in name or "history" in name:
            prior_rugs = safe_get(r, "description", default=NA)
            break

    return {
        "address": creator or NA,
        "dev_pct": dev_pct,
        "dev_sold": dev_sold,
        "history_note": prior_rugs,
    }


def analyze_holders(rug: dict) -> dict:
    top_holders = safe_get(rug, "topHolders", default=None) or safe_get(rug, "holders", default=None) or []
    top10_pct = None
    if isinstance(top_holders, list) and top_holders:
        try:
            top10_pct = sum(float(safe_get(h, "pct", default=0) or 0) for h in top_holders[:10])
        except (TypeError, ValueError):
            top10_pct = None

    snipers_note = NA
    bundled = None
    risks = safe_get(rug, "risks", default=[]) or []
    for r in risks:
        name = str(safe_get(r, "name", default="")).lower()
        if "snip" in name or "bundle" in name:
            bundled = True
            snipers_note = safe_get(r, "description", default="تم رصد قناصة/حزمة شراء بالبلوك الأول")
            break
    if bundled is None:
        bundled = False
        snipers_note = "لم يُرصد نمط قنص واضح في البيانات المتاحة"

    return {"top10_pct": top10_pct, "bundled": bundled, "snipers_note": snipers_note}


def analyze_security(rug: dict) -> dict:
    token = safe_get(rug, "token", default={}) or {}
    mint_auth = safe_get(token, "mintAuthority", default=_MISSING)
    if mint_auth is _MISSING:
        mint_auth = safe_get(rug, "mintAuthority", default=_MISSING)
    freeze_auth = safe_get(token, "freezeAuthority", default=_MISSING)
    if freeze_auth is _MISSING:
        freeze_auth = safe_get(rug, "freezeAuthority", default=_MISSING)

    mint_disabled = None if mint_auth is _MISSING else (mint_auth in (None, "", "null"))
    freeze_disabled = None if freeze_auth is _MISSING else (freeze_auth in (None, "", "null"))

    markets = safe_get(rug, "markets", default=[]) or []
    lp_locked_pct = None
    if markets:
        vals = [
            safe_get(m, "lp", "lpLockedPct", default=None)
            for m in markets
            if safe_get(m, "lp", "lpLockedPct", default=None) is not None
        ]
        if vals:
            try:
                lp_locked_pct = sum(float(v) for v in vals) / len(vals)
            except (TypeError, ValueError):
                lp_locked_pct = None

    score_raw = safe_get(rug, "score_normalised", default=None)
    if score_raw is None:
        score_raw = safe_get(rug, "score", default=None)

    return {
        "mint_disabled": mint_disabled,
        "freeze_disabled": freeze_disabled,
        "lp_locked_pct": lp_locked_pct,
        "rugcheck_score": score_raw,
    }


def analyze_momentum(dex: Optional[dict]) -> dict:
    if not dex:
        return {
            "vol_5m": None, "vol_1h": None, "liq_usd": None, "mcap": None,
            "buys_5m": None, "sells_5m": None, "change_5m": None, "change_1h": None,
            "price": None, "pair_age_ms": None, "pair_address": None,
        }
    buys_5m = to_float(safe_get(dex, "txns", "m5", "buys", default=None))
    sells_5m = to_float(safe_get(dex, "txns", "m5", "sells", default=None))
    return {
        "vol_5m": to_float(safe_get(dex, "volume", "m5", default=None)),
        "vol_1h": to_float(safe_get(dex, "volume", "h1", default=None)),
        "liq_usd": to_float(safe_get(dex, "liquidity", "usd", default=None)),
        "mcap": to_float(safe_get(dex, "marketCap", default=None)) or to_float(safe_get(dex, "fdv", default=None)),
        # نحافظ على القيم الصحيحة (int) لعدد العمليات بدل float لعرض أجمل (40 لا 40.0)
        "buys_5m": int(buys_5m) if buys_5m is not None else None,
        "sells_5m": int(sells_5m) if sells_5m is not None else None,
        "change_5m": to_float(safe_get(dex, "priceChange", "m5", default=None)),
        "change_1h": to_float(safe_get(dex, "priceChange", "h1", default=None)),
        "price": to_float(safe_get(dex, "priceUsd", default=None)),
        "pair_age_ms": to_float(safe_get(dex, "pairCreatedAt", default=None)),
        "pair_address": safe_get(dex, "pairAddress", default=None),
    }


# ------------------------------------------------------------------------- #
#  الخوارزمية: تقييم من 100
# ------------------------------------------------------------------------- #

def compute_score(security: dict, holders: dict, momentum: dict, dev: dict) -> dict:
    score = 0.0
    reasons = []

    # 1) الأمان: 40 نقطة
    sec_pts = 0.0
    if security["mint_disabled"] is True:
        sec_pts += 12
    elif security["mint_disabled"] is False:
        reasons.append("صلاحية السك مفعّلة (خطر تضخيم العرض)")
    if security["freeze_disabled"] is True:
        sec_pts += 12
    elif security["freeze_disabled"] is False:
        reasons.append("صلاحية التجميد مفعّلة (خطر منع البيع)")
    lp = security["lp_locked_pct"]
    if lp is not None:
        sec_pts += min(16, (lp / 100) * 16)
        if lp < 50:
            reasons.append("نسبة قفل/حرق السيولة منخفضة")
    score += sec_pts

    # 2) توزيع الحاملين: 25 نقطة
    dist_pts = 0.0
    t10 = holders["top10_pct"]
    if t10 is not None:
        if t10 <= 15:
            dist_pts += 18
        elif t10 <= 25:
            dist_pts += 11
        elif t10 <= 40:
            dist_pts += 5
            reasons.append("تركيز عالٍ نسبياً في أكبر 10 محافظ")
        else:
            reasons.append("تجميع خطير في أكبر 10 محافظ (أكثر من 40%)")
    if holders["bundled"] is False:
        dist_pts += 7
    elif holders["bundled"] is True:
        reasons.append("رُصد نمط قنص/حزمة شراء بالبلوك الأول")
    if dev.get("dev_sold") is True:
        dist_pts += 0  # المطور باع: لا عقوبة إضافية هنا (أحياناً إيجابي)، لكن ينذكر في التقرير
    elif dev.get("dev_sold") is False and (dev.get("dev_pct") or 0) > 15:
        reasons.append("المطوّر لا يزال يملك نسبة كبيرة من العرض")
    score += dist_pts

    # 3) الحجم ونسبة الشراء/البيع: 20 نقطة
    vol_pts = 0.0
    liq = momentum["liq_usd"] or 0
    vol5 = momentum["vol_5m"]
    buys, sells = momentum["buys_5m"], momentum["sells_5m"]
    if liq and vol5 is not None:
        ratio = vol5 / liq if liq else 0
        if 0.05 <= ratio <= 3:
            vol_pts += 10
        elif ratio > 3:
            reasons.append("حجم التداول ضخم جداً مقابل السيولة (قد يكون مصطنعاً)")
    if buys is not None and sells is not None:
        total = buys + sells
        if total >= 20:
            bratio = buys / max(sells, 1)
            if bratio >= 1.2:
                vol_pts += 10
            elif bratio >= 0.8:
                vol_pts += 5
            else:
                reasons.append("البيع يفوق الشراء في آخر 5 دقائق")
        else:
            reasons.append("عدد صفقات قليل جداً آخر 5 دقائق")
    score += vol_pts

    # 4) كفاية السيولة: 15 نقطة
    liq_pts = 0.0
    if liq:
        if liq >= 50_000:
            liq_pts += 15
        elif liq >= 15_000:
            liq_pts += 9
        elif liq >= 5_000:
            liq_pts += 4
            reasons.append("سيولة منخفضة، انزلاق سعري متوقع")
        else:
            reasons.append("سيولة ضعيفة جداً، خطر مرتفع")
    score += liq_pts

    # 5) تأكيد الاتجاه (Trend Confirmation): عقوبة صريحة إذا السعر فعلاً هابط
    # بقوة رغم أن باقي المحاور (أمان/توزيع/سيولة) تبان جيدة. هذا كان ثغرة
    # حقيقية: عملة ممكن تكون "نظيفة" من ناحية العقد لكنها أصلاً في موجة هبوط،
    # وبلا هذا الفحص كانت تاخذ تقييم عالي رغم إنها فعلياً "تروقة" سعرية جارية.
    change_1h = momentum.get("change_1h")
    change_5m = momentum.get("change_5m")
    if change_1h is not None:
        if change_1h <= -25:
            score -= 15
            reasons.append("هبوط حاد جداً آخر ساعة، الموجة غالباً انتهت أو العملة تنهار")
        elif change_1h <= -10:
            score -= 7
            reasons.append("ضعف واضح في السعر آخر ساعة")
    if change_5m is not None and change_1h is not None and change_1h > 0 and change_5m <= -8:
        # الاتجاه العام صاعد لكن آخر 5 دقايق فيها انعكاس بيع قوي = إشارة خروج مبكرة
        score -= 5
        reasons.append("انعكاس بيع قصير المدى رغم الاتجاه الصاعد العام")

    total = round(min(100, max(0, score)))
    if total >= 75:
        verdict = "🚀 فرصة سكالپينغ ممتازة"
    elif total >= 45:
        verdict = "⚠️ منطقة مخاطرة"
    else:
        verdict = "🔴 خطر - لا تشتري!"

    main_reason = reasons[0] if reasons else "لا توجد ملاحظات حرجة من الفحوصات المتاحة"
    return {"total": total, "verdict": verdict, "main_reason": main_reason, "all_reasons": reasons}


# ------------------------------------------------------------------------- #
#  محرك السكالبينغ: منطقة الدخول، الأهداف، ووقف الخسارة
# ------------------------------------------------------------------------- #

# نسبة وقف الخسارة تحت قاع الموجة مباشرة (داخل مجال 1%-2% المطلوب)
SCALP_SL_BUFFER_PCT = 0.015
# منطقة الدخول الذهبية: تصحيح فيبوناتشي بين 0.5 و0.618 من الموجة
FIB_ENTRY_DEEP = 0.618   # الحد الأدنى للمنطقة (تصحيح أعمق)
FIB_ENTRY_SHALLOW = 0.50  # الحد الأعلى للمنطقة (تصحيح أخف)
# امتدادات فيبوناتشي للأهداف، مقاسة من قاع الموجة
FIB_TP1_EXT = 1.272
FIB_TP2_EXT = 1.618


def compute_scalp_plan(price: Optional[float], swing: Optional[dict],
                        liq_usd: Optional[float], mcap: Optional[float] = None) -> Optional[dict]:
    """يبني خطة سكالبينغ على Price Action حقيقي لآخر موجة فقط (Recent Swing Wave):

    1. منطقة الدخول = "Golden Zone" — تصحيح فيبوناتشي 0.5-0.618 من قاع الموجة
       الأخيرة (swing_low) إلى قمتها الحالية (swing_high)، مو من أي قاع تاريخي.
    2. وقف الخسارة = تحت قاع الموجة مباشرة بـ1.5% (ضمن مجال 1-2% المطلوب).
    3. الأهداف = امتدادات فيبوناتشي 1.272 و1.618 مقاسة من نفس الموجة، مو
       نسبة ثابتة بلا علاقة بحجم الحركة الفعلي.
    4. "حالة المنطقة": هل السعر الآن داخل منطقة الدخول، لسا فوقها (ننتظر
       تصحيح)، أو كسر تحت الدعم (الإعداد بطل صالح)؟

    إذا ما توفرتش بيانات شموع موثوقة (swing=None)، نرجع لتقدير تقريبي بسيط
    معلن بوضوح في التقرير (estimated=True) بدل ما نرفض الخطة كلياً.
    """
    if not price or price <= 0:
        return None

    estimated = swing is None
    if swing:
        swing_low, swing_high = swing["swing_low"], swing["swing_high"]
        timeframe = swing.get("timeframe", "5m")
    else:
        # احتياطي بسيط: نفترض موجة وهمية حول السعر الحالي حتى يبقى للخطة معنى
        swing_low, swing_high = price * 0.85, price * 1.05
        timeframe = NA

    wave = swing_high - swing_low
    if wave <= 0:
        return None

    entry_low = swing_high - wave * FIB_ENTRY_DEEP      # تصحيح 0.618 (أعمق)
    entry_high = swing_high - wave * FIB_ENTRY_SHALLOW  # تصحيح 0.50 (أخف)
    stop_loss = swing_low * (1 - SCALP_SL_BUFFER_PCT)
    tp1 = swing_low + wave * FIB_TP1_EXT
    tp2 = swing_low + wave * FIB_TP2_EXT

    risk = entry_low - stop_loss
    reward = tp1 - entry_low
    rr = (reward / risk) if risk > 0 else None

    # حالة منطقة الدخول بالنسبة للسعر الحالي الآن
    if price < stop_loss:
        zone_status = "🔴 السعر كسر تحت الدعم — هذا الإعداد بطل صالح، لا تدخل."
    elif price <= entry_high:
        zone_status = "🟢 السعر داخل منطقة الدخول حالياً."
    else:
        zone_status = "⏳ السعر لسا فوق منطقة الدخول — استنى تصحيح للمنطقة قبل الدخول."

    liq_note = None
    if liq_usd is not None and liq_usd < 15_000:
        liq_note = "⚠️ السيولة منخفضة: انزلاق السعر (Slippage) قد يبعدك عن هذه المستويات بالضبط."

    # تحويل لماركت كاب: نفترض عرض متداول ثابت (صحيح طالما Mint Authority ملغاة)
    supply = None
    if mcap and price and price > 0:
        try:
            supply = float(mcap) / price
        except (TypeError, ValueError, ZeroDivisionError):
            supply = None

    def to_mcap(p):
        return p * supply if supply else None

    return {
        "entry_low": entry_low, "entry_high": entry_high,
        "stop_loss": stop_loss,
        "tp1": tp1, "tp2": tp2,
        "swing_low": swing_low, "swing_high": swing_high, "timeframe": timeframe,
        "zone_status": zone_status,
        "rr": rr, "estimated": estimated, "liq_note": liq_note,
        "mcap_entry_low": to_mcap(entry_low), "mcap_entry_high": to_mcap(entry_high),
        "mcap_stop_loss": to_mcap(stop_loss),
        "mcap_tp1": to_mcap(tp1), "mcap_tp2": to_mcap(tp2),
    }


# ------------------------------------------------------------------------- #
#  بناء التقرير النهائي (عربي)
# ------------------------------------------------------------------------- #

def green_red(cond: Optional[bool], yes_label: str, no_label: str) -> str:
    if cond is None:
        return f"❓ {NA}"
    return f"🟢 {yes_label}" if cond else f"🔴 {no_label}"


def build_scalp_section(rating: dict, scalp: Optional[dict]) -> str:
    if rating["total"] < SCALP_MIN_SCORE:
        reasons = rating["all_reasons"][:3] or ["التقييم العام تحت الحد الأدنى للسكالبينغ الآمن"]
        bullet_reasons = "\n".join(f"  ▫️ {r}" for r in reasons)
        return (
            "⚡ *خطة السكالبينغ والتداول (Scalp Setup):*\n"
            f"🔴 *لا توجد خطة دخول* — التقييم ({rating['total']}/100) تحت الحد الآمن ({SCALP_MIN_SCORE}+).\n"
            f"*أسباب التحذير:*\n{bullet_reasons}"
        )
    if not scalp:
        return (
            "⚡ *خطة السكالبينغ والتداول (Scalp Setup):*\n"
            "⚠️ التقييم إيجابي لكن ما كفاش بيانات سعرية موثوقة لحساب منطقة دخول دقيقة."
        )

    tf_label = {"5m": "شموع 5 دقائق", "1m": "شموع دقيقة واحدة"}.get(scalp["timeframe"], scalp["timeframe"])
    est_tag = (
        " _(تقدير تقريبي، ما قدرناش نجيب شموع موثوقة)_"
        if scalp["estimated"]
        else f" _(مبني على Golden Zone لآخر موجة — {tf_label})_"
    )
    rr_line = f"1:{scalp['rr']:.1f}" if scalp["rr"] else NA

    def level(label, p, mp, extra_pct=None):
        s = f"{label} {fmt_price(p)}"
        if extra_pct is not None:
            s += f" ({'+' if extra_pct >= 0 else ''}{extra_pct:.0f}%)"
        if mp is not None:
            s += f"\n   💠 {fmt_usd(mp)}"
        return s

    entry_mid = (scalp["entry_low"] + scalp["entry_high"]) / 2
    tp1_pct = (scalp["tp1"] / entry_mid - 1) * 100 if entry_mid else None
    tp2_pct = (scalp["tp2"] / entry_mid - 1) * 100 if entry_mid else None
    sl_pct = (scalp["stop_loss"] / entry_mid - 1) * 100 if entry_mid else None

    lines = [
        "⚡ *خطة السكالبينغ والتداول (Scalp Setup):*",
        f"📐 *الموجة الأخيرة:* قاع {fmt_price(scalp['swing_low'])} ← قمة {fmt_price(scalp['swing_high'])}",
        f"{scalp['zone_status']}",
        f"🎯 *منطقة الدخول (Golden Zone 0.5-0.618):* {fmt_price(scalp['entry_low'])} — {fmt_price(scalp['entry_high'])}{est_tag}"
        + (f"\n   💠 {fmt_usd(scalp['mcap_entry_low'])} — {fmt_usd(scalp['mcap_entry_high'])}" if scalp['mcap_entry_low'] is not None else ""),
        f"🚀 {level('*الهدف الأول (TP1 — امتداد 1.272):*', scalp['tp1'], scalp['mcap_tp1'], tp1_pct)}",
        f"🚀 {level('*الهدف الثاني (TP2 — امتداد 1.618):*', scalp['tp2'], scalp['mcap_tp2'], tp2_pct)}",
        f"🛑 {level('*وقف الخسارة (تحت قاع الموجة بـ1.5%):*', scalp['stop_loss'], scalp['mcap_stop_loss'], sl_pct)}",
        f"⚖️ *نسبة المخاطرة/العائد:* {rr_line}",
    ]
    if scalp["liq_note"]:
        lines.append(scalp["liq_note"])
    return "\n".join(lines)


def build_report(mint: str, name: str, symbol: str, origin: dict, dev: dict,
                  holders: dict, security: dict, momentum: dict, rating: dict,
                  scalp: Optional[dict] = None) -> str:

    dev_line = green_red(dev.get("dev_sold"), "باع كل حصته", f"ما زال يملك {dev.get('dev_pct', 0) or 0:.1f}%")
    top10_line = NA
    if holders["top10_pct"] is not None:
        tag = "🟢 موزعة جيداً" if holders["top10_pct"] <= 25 else "🔴 تجميع خطير"
        top10_line = f"{holders['top10_pct']:.1f}% من الإمداد [{tag}]"
    snipers_line = "تم التخلص منهم ✅" if holders["bundled"] is False else ("ما زالوا يتحكمون ⚠️" if holders["bundled"] else NA)

    mint_line = green_red(security["mint_disabled"], "ملغاة", "مفعلة - خطر")
    freeze_line = green_red(security["freeze_disabled"], "ملغاة", "مفعلة - خطر")
    lp_line = f"{security['lp_locked_pct']:.0f}%" if security["lp_locked_pct"] is not None else NA
    rc_score_line = f"{security['rugcheck_score']}" if security["rugcheck_score"] is not None else NA

    bs_ratio = NA
    if momentum["buys_5m"] is not None and momentum["sells_5m"] is not None:
        bratio = momentum["buys_5m"] / max(momentum["sells_5m"], 1)
        bs_ratio = f"{momentum['buys_5m']} شراء / {momentum['sells_5m']} بيع (النسبة: {bratio:.1f})"

    report = f"""
🪙 *{name}* (${symbol})
📍 *المصدر:* {origin['source']}
💰 *السعر الحالي:* {fmt_price(momentum['price'])} | *القيمة السوقية:* {fmt_usd(momentum['mcap'])} | *السيولة:* {fmt_usd(momentum['liq_usd'])}
⏱ *عمر العملة:* {age_from_ms(momentum['pair_age_ms'])}

👑 *تحليل المطور والحيتان (Dev & Holders):*
• *المطور (Dev):* {dev_line}
• *أكبر 10 كبار الملاك (Top 10):* {top10_line}
• *القناصة (Snipers/Bundles):* {snipers_line}

🛡 *الأمان والسيولة (RugCheck):*
• *خاصية السك (Mint):* {mint_line}
• *خاصية التجميد (Freeze):* {freeze_line}
• *حرق/قفل السيولة (LP):* {lp_line}
• *تقييم RugCheck الخام:* {rc_score_line}

📈 *الحركة وحجم التداول:*
• *حجم التداول (5 د):* {fmt_usd(momentum['vol_5m'])} | *(1 س):* {fmt_usd(momentum['vol_1h'])}
• *العمليات (5 د):* {bs_ratio}
• *تغيّر السعر:* 5د {fmt_pct(momentum['change_5m'], signed=True)} | 1س {fmt_pct(momentum['change_1h'], signed=True)}

🎯 *التقييم النهائي: {rating['total']}/100*
• *التوصية:* {rating['verdict']}
• *السبب الرئيسي:* {rating['main_reason']}

{build_scalp_section(rating, scalp)}

`{mint}`

⚠️ _هذا تحليل آلي لأغراض المعلومات فقط، وليس توصية مالية مضمونة. القرار والمسؤولية ترجع لك بالكامل._
""".strip()
    return report


# ------------------------------------------------------------------------- #
#  منطق التحليل الكامل لعملة واحدة
# ------------------------------------------------------------------------- #

async def analyze_mint(address: str) -> str:
    async with aiohttp.ClientSession() as session:
        dex, rug, pump = await asyncio.gather(
            fetch_dexscreener(session, address),
            fetch_rugcheck(session, address),
            fetch_pumpfun(session, address),
        )

        # Universal Data Fallback: العملة جد جديدة وما وصلتش بعد لـ DexScreener
        # (أو المستخدم دخل عنوان عملة ما نجحش يتربط ببركة) -> نجرب GeckoTerminal.
        used_fallback = False
        if not dex:
            dex = await fetch_geckoterminal(session, address)
            used_fallback = dex is not None

        if not dex and not rug and not pump:
            return (
                "⚠️ ما قدرتش نلقى أي بيانات لهذا العقد.\n"
                "تأكد من صحة العنوان (Token Mint أو Pair Address)، أو العملة جد جديدة "
                "وما زالت البيانات ما توصلتش لأي مصدر بعد."
            )

        name = (
            safe_get(dex, "baseToken", "name", default=None)
            or safe_get(pump, "name", default=None)
            or "عملة غير معروفة"
        )
        symbol = (
            safe_get(dex, "baseToken", "symbol", default=None)
            or safe_get(pump, "symbol", default=None)
            or "???"
        )

        origin = analyze_origin(dex, pump)
        dev = analyze_dev(rug, pump)
        holders = analyze_holders(rug)
        security = analyze_security(rug)
        momentum = analyze_momentum(dex)
        rating = compute_score(security, holders, momentum, dev)

        # خطة السكالبينغ تحتاج قاع سعري حقيقي؛ نجيبه فقط إذا التقييم يستاهل
        # (يوفر طلبات شبكة غير ضرورية على العملات الضعيفة أصلاً).
        scalp = None
        if rating["total"] >= SCALP_MIN_SCORE and momentum["price"]:
            swing = await fetch_swing_wave(session, momentum.get("pair_address"))
            scalp = compute_scalp_plan(momentum["price"], swing, momentum["liq_usd"], momentum.get("mcap"))

    if used_fallback:
        log.info("Used GeckoTerminal fallback for %s", address)

    return build_report(address, name, symbol, origin, dev, holders, security, momentum, rating, scalp)


# ------------------------------------------------------------------------- #
#  معالجات تيليغرام
# ------------------------------------------------------------------------- #

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 أهلاً بك في *Solana Deep Scan & Scalp Bot v2.0*.\n\n"
        "ابعثلي عنوان عملة (Token Mint) أو عنوان بركة (Pair/Pool Address) على "
        "Pump.fun أو Raydium، ونرجّعلك تقرير تحليل كامل: الأمان، المطوّر، "
        "الحاملين، والحجم — وإذا كانت العملة قوية، خطة سكالبينغ جاهزة.\n\n"
        "مثال:\n`EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_mint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mint = (update.message.text or "").strip()
    if not SOLANA_MINT_RE.match(mint):
        await update.message.reply_text(
            "❌ هذا ما يشبه عنوان عقد سولانا صحيح. تأكد وأعد المحاولة."
        )
        return

    status_msg = await update.message.reply_text("🔎 جاري التحليل العميق، لحظات من فضلك...")
    try:
        report = await analyze_mint(mint)
    except Exception:  # pylint: disable=broad-except
        log.exception("Unhandled error analyzing mint %s", mint)
        report = "❌ صار خطأ غير متوقع أثناء التحليل. حاول مرة أخرى بعد شوية."

    try:
        await status_msg.edit_text(report, parse_mode=ParseMode.MARKDOWN)
    except Exception:  # fallback إذا فشل تنسيق Markdown (أحرف خاصة في الاسم مثلاً)
        await status_msg.edit_text(report)


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "ابعثلي عنوان عقد سولانا صحيح (Mint Address) باش نحلله ليك. اكتب /start للمساعدة."
    )


# ------------------------------------------------------------------------- #
#  نقطة الانطلاق
# ------------------------------------------------------------------------- #

def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("خطأ: متغيّر البيئة TELEGRAM_BOT_TOKEN غير موجود.")

    # خادم Health Check في Thread منفصل حتى يبقى البوت "حي" في نظر Render
    threading.Thread(target=run_health_server, args=(PORT,), daemon=True).start()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(MessageHandler(filters.Regex(SOLANA_MINT_RE), handle_mint))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown))

    log.info("Bot started. Polling for updates...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
