#!/usr/bin/env python3
"""
Solana Deep Scan & Scalp Bot v2.0 "Jabar Edition"

يفحص أي عنوان (Token Mint Address أو Pair/Pool Address) على سولانا عبر:
  - DexScreener    (المصدر الأساسي: السعر، السيولة، الحجم، البيع/الشراء)
  - GeckoTerminal  (مصدر احتياطي تلقائي للعملات الجديدة جداً اللي ما توصلتش لـ DexScreener بعد)
  - RugCheck       (أمان العقد، توزيع الحاملين، حرق/قفل السيولة)
  - Pump.fun       (مصدر العملة، محفظة المطوّر، حالة الهجرة لـ Raydium)

يحسب تقييماً من 100، وإذا كانت العملة قوية (>=70) يولّد خطة سكالبينغ كاملة
(منطقة دخول، هدفين، وقف خسارة، نسبة مخاطرة/عائد) مبنية على قاع سعري حقيقي
مستخرج من بيانات GeckoTerminal OHLCV، ثم يرسل تقرير عربي منسّق على تيليغرام.

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
GECKOTERMINAL_OHLCV_URL = (
    "https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/hour"
    "?aggregate=1&limit=48&currency=usd"
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


async def fetch_recent_low(session: aiohttp.ClientSession, pool_address: Optional[str]) -> Optional[float]:
    """يجيب قاع السعر الفعلي على آخر 48 ساعة من شموع GeckoTerminal (OHLCV).

    يُستعمل فقط لحساب منطقة الدعم في خطة السكالبينغ. يرجّع None عند أي فشل
    (مسار غير موجود، عنوان بركة مفقود...)، وعندها يتحول الحساب لتقدير تقريبي.
    """
    if not pool_address:
        return None
    data = await fetch_json(session, GECKOTERMINAL_OHLCV_URL.format(pool=pool_address))
    rows = safe_get(data, "data", "attributes", "ohlcv_list", default=None)
    if not rows:
        return None
    lows = []
    for row in rows:
        try:
            lows.append(float(row[3]))  # [timestamp, open, high, low, close, volume]
        except (IndexError, TypeError, ValueError):
            continue
    return min(lows) if lows else None


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
            dev_pct = safe_get(match, "pct", default=None)
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
    price = safe_get(dex, "priceUsd", default=None)
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None
    return {
        "vol_5m": safe_get(dex, "volume", "m5", default=None),
        "vol_1h": safe_get(dex, "volume", "h1", default=None),
        "liq_usd": safe_get(dex, "liquidity", "usd", default=None),
        "mcap": safe_get(dex, "marketCap", default=None) or safe_get(dex, "fdv", default=None),
        "buys_5m": safe_get(dex, "txns", "m5", "buys", default=None),
        "sells_5m": safe_get(dex, "txns", "m5", "sells", default=None),
        "change_5m": safe_get(dex, "priceChange", "m5", default=None),
        "change_1h": safe_get(dex, "priceChange", "h1", default=None),
        "price": price,
        "pair_age_ms": safe_get(dex, "pairCreatedAt", default=None),
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

def compute_scalp_plan(price: Optional[float], recent_low: Optional[float],
                        liq_usd: Optional[float]) -> Optional[dict]:
    """يبني خطة سكالبينغ كاملة حول "منطقة دعم" محسوبة.

    منطقة الدعم = قاع سعري حقيقي من آخر 48 ساعة (GeckoTerminal OHLCV) إذا
    توفر، وإلا تقدير تقريبي (-10% من السعر الحالي) كحل احتياطي معلن بوضوح
    في التقرير. هذا حساب رياضي بسيط على بيانات حقيقية، وليس توصية مضمونة.
    """
    if not price or price <= 0:
        return None

    estimated = recent_low is None
    support = recent_low if (recent_low and 0 < recent_low < price) else price * 0.90

    # إذا القاع قريب جداً من السعر الحالي (فرق أقل من 2%)، نوسّع هامش الأمان
    # حتى يبقى وقف الخسارة له معنى عملي.
    if price > 0 and (price - support) / price < 0.02:
        support = price * 0.95
        estimated = True

    entry_low = support * 1.00
    entry_high = support * 1.02
    stop_loss = support * (1 - 0.04)  # 4% تحت الدعم (داخل مجال 3-5% المطلوب)
    tp1_low, tp1_high = entry_low * 1.15, entry_high * 1.20
    tp2_low, tp2_high = entry_low * 1.35, entry_high * 1.50

    risk = entry_low - stop_loss
    reward = tp1_low - entry_low
    rr = (reward / risk) if risk > 0 else None

    liq_note = None
    if liq_usd is not None and liq_usd < 15_000:
        liq_note = "⚠️ السيولة منخفضة: انزلاق السعر (Slippage) قد يبعدك عن هذه المستويات بالضبط."

    return {
        "entry_low": entry_low, "entry_high": entry_high,
        "stop_loss": stop_loss,
        "tp1_low": tp1_low, "tp1_high": tp1_high,
        "tp2_low": tp2_low, "tp2_high": tp2_high,
        "rr": rr, "estimated": estimated, "liq_note": liq_note,
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

    est_tag = " _(تقدير تقريبي، ما كانش قاع سعري مؤكد)_" if scalp["estimated"] else " _(مبني على قاع سعري فعلي 48 ساعة)_"
    rr_line = f"1:{scalp['rr']:.1f}" if scalp["rr"] else NA
    lines = [
        "⚡ *خطة السكالبينغ والتداول (Scalp Setup):*",
        f"🎯 *منطقة الدخول:* {fmt_price(scalp['entry_low'])} — {fmt_price(scalp['entry_high'])}{est_tag}",
        f"🚀 *الهدف الأول (TP1):* {fmt_price(scalp['tp1_low'])} — {fmt_price(scalp['tp1_high'])} (+15% إلى +20%)",
        f"🚀 *الهدف الثاني (TP2):* {fmt_price(scalp['tp2_low'])} — {fmt_price(scalp['tp2_high'])} (+35% إلى +50%)",
        f"🛑 *وقف الخسارة:* {fmt_price(scalp['stop_loss'])} (تحت الدعم بـ4%)",
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
            recent_low = await fetch_recent_low(session, momentum.get("pair_address"))
            scalp = compute_scalp_plan(momentum["price"], recent_low, momentum["liq_usd"])

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
