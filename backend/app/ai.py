"""AI analysis: fetches recent news per ticker and asks a model for a
swing-trade read (summary, sentiment, risks/catalysts, confidence).

Optimizations:
  * news is prefetched concurrently so the model never waits on the network
  * analyses run with bounded concurrency (not one-at-a-time)
  * only the top-N setups are analyzed automatically; the rest are analyzed
    on demand via analyze_single()

Providers (set AI_PROVIDER in .env):
  - ollama     free local model via Ollama (default)
  - anthropic  Claude API (paid, needs ANTHROPIC_API_KEY)
  - none       skip AI analysis entirely
"""

import asyncio
import json
import logging
import os

import httpx
import yfinance as yf

from .indicators import rsi, sma

log = logging.getLogger(__name__)

ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_OLLAMA_MODEL = "llama3.2:3b"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
ANTHROPIC_CONCURRENCY = 4
DEFAULT_OLLAMA_CONCURRENCY = 2  # safe on an 8 GB M1 with a 3B model
NEWS_CONCURRENCY = 8
MAX_NEWS_ITEMS = 8

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "2-3 sentence plain-English summary of what's going on with the stock right now.",
        },
        "sentiment": {"type": "string", "enum": ["Bullish", "Neutral", "Bearish"]},
        "risks_catalysts": {
            "type": "string",
            "description": "Notable risks or upcoming catalysts worth knowing, in 1-2 sentences.",
        },
        "confidence": {
            "type": "string",
            "enum": ["High", "Medium", "Low"],
            "description": "Overall confidence based on how well the technical setup and news sentiment align.",
        },
    },
    "required": ["summary", "sentiment", "risks_catalysts", "confidence"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- news


def _fetch_news(ticker: str) -> list[dict]:
    """Pull recent headlines from yfinance. Handles both old and new news shapes."""
    try:
        items = yf.Ticker(ticker).news or []
    except Exception:
        log.exception("News fetch failed for %s", ticker)
        return []

    news = []
    for item in items[:MAX_NEWS_ITEMS]:
        content = item.get("content") if isinstance(item.get("content"), dict) else item
        title = content.get("title")
        if not title:
            continue
        provider = content.get("provider")
        news.append(
            {
                "title": title,
                "summary": content.get("summary") or content.get("description") or "",
                "published": content.get("pubDate") or "",
                "source": provider.get("displayName", "") if isinstance(provider, dict) else "",
            }
        )
    return news


async def _prefetch_news(tickers: list[str]) -> dict[str, list[dict]]:
    """Fetch every ticker's news concurrently so the model loop never stalls on I/O."""
    sem = asyncio.Semaphore(NEWS_CONCURRENCY)

    async def one(ticker: str):
        async with sem:
            return ticker, await asyncio.to_thread(_fetch_news, ticker)

    return dict(await asyncio.gather(*(one(t) for t in tickers)))


def _build_prompt(stock: dict, news: list[dict]) -> str:
    if news:
        news_block = "\n".join(
            f"- [{n['source'] or 'unknown'}] {n['title']}"
            + (f" — {n['summary'][:300]}" if n["summary"] else "")
            for n in news
        )
    else:
        news_block = "(no recent news found for this ticker)"

    return f"""You are assisting a swing trader who holds positions for 2-5 days. The stock below just passed a technical scan (a market leader in a confirmed uptrend, currently pulled back: high relative strength, near its 52-week high, price above the 50 & 200 SMA, RSI in a healthy pullback band).

Ticker: {stock['ticker']}
Current price: ${stock['price']}
Relative strength rank: {stock.get('rs_rating', '?')}/100 (vs the scanned universe)
Distance from 52-week high: {stock.get('pct_from_high', '?')}% below
RSI(14): {stock['rsi']} (pulled back)
ADX(14): {stock['adx']} (trend strength)
ATR%: {stock['atr_pct']}% (daily volatility)
Relative volume: {stock['rel_volume']}x its 21-day average
20-day SMA: ${stock['sma20']}
50-day SMA: ${stock['sma50']} (price is {stock['pct_above_sma50']}% above it)
200-day SMA: ${stock.get('sma200', '?')}
21-day average volume: {stock['avg_volume']:,}

Recent news headlines:
{news_block}

Based on the news and the technical setup, provide:
1. A 2-3 sentence plain-English summary of what's going on with this stock.
2. A sentiment call: Bullish, Neutral, or Bearish for a 2-5 day hold.
3. Notable risks or catalysts worth knowing (earnings dates, lawsuits, product launches, macro exposure, etc.).
4. An overall confidence rating (High / Medium / Low) based on how well the technicals and news sentiment align. If there is no news, lean on the technicals and cap confidence at Medium.

Write everything in your own words — never copy headlines verbatim or include source names in brackets."""


def _fallback(message: str) -> dict:
    return {
        "summary": message,
        "sentiment": "Neutral",
        "risks_catalysts": "",
        "confidence": "Low",
        "news_count": 0,
        "error": True,
    }


def _normalize(analysis: dict) -> dict:
    """Small local models occasionally drift outside the enums — clamp them."""
    if analysis.get("sentiment") not in ("Bullish", "Neutral", "Bearish"):
        analysis["sentiment"] = "Neutral"
    if analysis.get("confidence") not in ("High", "Medium", "Low"):
        analysis["confidence"] = "Low"
    analysis.setdefault("summary", "")
    analysis.setdefault("risks_catalysts", "")
    return analysis


# ---------------------------------------------------------------- provider config


def _provider() -> str:
    return os.environ.get("AI_PROVIDER", "ollama").strip().lower()


def _ollama_url() -> str:
    return os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL).rstrip("/")


def _ollama_model() -> str:
    return os.environ.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)


def _ollama_concurrency() -> int:
    try:
        return max(1, int(os.environ.get("OLLAMA_CONCURRENCY", str(DEFAULT_OLLAMA_CONCURRENCY))))
    except ValueError:
        return DEFAULT_OLLAMA_CONCURRENCY


def _anthropic_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    return "" if key == "sk-ant-..." else key  # treat the .env.example placeholder as unset


# ---------------------------------------------------------------- per-stock model calls


async def _ollama_call(client: httpx.AsyncClient, model: str, stock: dict, news: list[dict]) -> dict:
    try:
        resp = await client.post(
            f"{_ollama_url()}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": _build_prompt(stock, news)}],
                "stream": False,
                "format": ANALYSIS_SCHEMA,  # Ollama structured outputs (v0.5+)
                "options": {"temperature": 0.3},
            },
        )
        resp.raise_for_status()
        analysis = json.loads(resp.json()["message"]["content"])
        analysis["news_count"] = len(news)
        return _normalize(analysis)
    except Exception:
        log.exception("Ollama analysis failed for %s", stock["ticker"])
        return _fallback("Local AI analysis failed — see backend logs.")


async def _anthropic_call(client, stock: dict, news: list[dict]) -> dict:
    import anthropic

    try:
        response = await client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1024,
            output_config={"format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA}},
            messages=[{"role": "user", "content": _build_prompt(stock, news)}],
        )
        text = next(b.text for b in response.content if b.type == "text")
        analysis = json.loads(text)
        analysis["news_count"] = len(news)
        return analysis
    except anthropic.APIError as e:
        log.error("Claude analysis failed for %s: %s", stock["ticker"], e)
        return _fallback(f"AI analysis failed ({e.__class__.__name__}). Check your API key and credits.")
    except Exception:
        log.exception("Unexpected AI failure for %s", stock["ticker"])
        return _fallback("AI analysis failed unexpectedly — see backend logs.")


async def _ollama_preflight(client: httpx.AsyncClient) -> str | None:
    """Return an error message if Ollama isn't usable, else None."""
    model = _ollama_model()
    try:
        resp = await client.get(f"{_ollama_url()}/api/tags", timeout=3)
        resp.raise_for_status()
    except Exception:
        return (
            "Ollama isn't running. Install it (brew install ollama), start it "
            "(ollama serve), and pull a model: ollama pull " + model
        )
    installed = [m.get("name", "") for m in resp.json().get("models", [])]
    base = model.split(":")[0]
    if not any(name == model or name.split(":")[0] == base for name in installed):
        return f"Model '{model}' not found in Ollama. Run: ollama pull {model}"
    return None


# ---------------------------------------------------------------- batch analysis


async def _run_ollama(stocks: list[dict], news_map: dict, progress) -> None:
    model = _ollama_model()
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=5)) as client:
        error = await _ollama_preflight(client)
        if error:
            log.warning("Ollama unavailable: %s", error)
            for stock in stocks:
                stock["ai"] = _fallback(error)
            return

        sem = asyncio.Semaphore(_ollama_concurrency())
        done = 0

        async def worker(stock: dict):
            nonlocal done
            async with sem:
                stock["ai"] = await _ollama_call(client, model, stock, news_map.get(stock["ticker"], []))
                done += 1
                progress(f"AI analyzed {done}/{len(stocks)} (local {model})…")

        await asyncio.gather(*(worker(s) for s in stocks))


async def _run_anthropic(stocks: list[dict], news_map: dict, progress) -> None:
    import anthropic

    client = anthropic.AsyncAnthropic()
    sem = asyncio.Semaphore(ANTHROPIC_CONCURRENCY)
    done = 0

    async def worker(stock: dict):
        nonlocal done
        async with sem:
            stock["ai"] = await _anthropic_call(client, stock, news_map.get(stock["ticker"], []))
            done += 1
            progress(f"AI analyzed {done}/{len(stocks)} with Claude…")

    await asyncio.gather(*(worker(s) for s in stocks))


# ---------------------------------------------------------------- entry points


async def analyze_all(stocks: list[dict], progress=lambda msg: None, limit: int | None = None) -> None:
    """Analyze the top `limit` setups (by score; they're pre-sorted) in place.
    The remaining setups are flagged for on-demand analysis (analyze_single)."""
    if not stocks:
        return

    targets = stocks if limit is None else stocks[:limit]
    for stock in stocks[len(targets):]:
        if not stock.get("ai"):
            stock["ai_status"] = "idle"  # frontend shows an "Analyze" button
    if not targets:
        return

    provider = _provider()
    if provider == "none":
        for stock in targets:
            stock["ai"] = _fallback("AI analysis disabled (AI_PROVIDER=none).")
        return
    if provider == "anthropic" and not _anthropic_key():
        for stock in targets:
            stock["ai"] = _fallback("AI_PROVIDER=anthropic but no ANTHROPIC_API_KEY set in .env.")
        return

    progress(f"Fetching news & analyzing top {len(targets)} setups…")
    news_map = await _prefetch_news([s["ticker"] for s in targets])
    if provider == "anthropic":
        await _run_anthropic(targets, news_map, progress)
    else:
        await _run_ollama(targets, news_map, progress)


POSITION_SCHEMA = {
    "type": "object",
    "properties": {
        "insight": {
            "type": "string",
            "description": "ONE sentence on whether the technical setup still looks valid for an open swing position, or what has changed since entry.",
        },
        "status": {"type": "string", "enum": ["Valid", "Caution", "Broken"]},
    },
    "required": ["insight", "status"],
    "additionalProperties": False,
}


def _tech_snapshot(symbol: str) -> dict | None:
    """Quick current-technicals read for an open position."""
    try:
        df = yf.Ticker(symbol).history(period="1y", interval="1d", auto_adjust=True)
    except Exception:
        log.exception("Snapshot download failed for %s", symbol)
        return None
    if df is None or df.empty or len(df) < 60:
        return None
    close = df["Close"]
    price = float(close.iloc[-1])
    high_52w = float(df["High"].tail(252).max())
    return {
        "price": round(price, 2),
        "sma20": round(float(sma(close, 20).iloc[-1]), 2),
        "sma50": round(float(sma(close, 50).iloc[-1]), 2),
        "sma200": round(float(sma(close, 200).iloc[-1]), 2) if len(close) >= 200 else None,
        "rsi": round(float(rsi(close, 14).iloc[-1]), 1),
        "pct_from_high": round((high_52w - price) / high_52w * 100, 1) if high_52w else None,
    }


async def _provider_json(prompt: str, schema: dict) -> dict | None:
    """Single structured-output call against the configured provider. None on failure."""
    provider = _provider()
    if provider == "none":
        return None
    try:
        if provider == "anthropic":
            if not _anthropic_key():
                return None
            import anthropic

            resp = await anthropic.AsyncAnthropic().messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=512,
                output_config={"format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": prompt}],
            )
            return json.loads(next(b.text for b in resp.content if b.type == "text"))
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5)) as client:
            if await _ollama_preflight(client):
                return None
            r = await client.post(
                f"{_ollama_url()}/api/chat",
                json={
                    "model": _ollama_model(),
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "format": schema,
                    "options": {"temperature": 0.3},
                },
            )
            r.raise_for_status()
            return json.loads(r.json()["message"]["content"])
    except Exception:
        log.exception("Provider JSON call failed")
        return None


async def position_insight(position: dict) -> dict:
    """One-sentence read on whether an open position's setup still holds."""
    symbol = position.get("symbol", "?")
    snap = await asyncio.to_thread(_tech_snapshot, symbol)
    news = await asyncio.to_thread(_fetch_news, symbol)
    news_block = "\n".join(f"- {n['title']}" for n in news[:5]) or "(no recent news)"
    tech = (
        f"Now ${snap['price']} | RSI {snap['rsi']} | 20SMA ${snap['sma20']} | 50SMA ${snap['sma50']} "
        f"| 200SMA ${snap['sma200']} | {snap['pct_from_high']}% below 52w high"
        if snap
        else "(technical snapshot unavailable)"
    )
    prompt = f"""You are reviewing an OPEN swing-trade position (2-5 day hold). Decide in ONE sentence whether the technical setup still looks valid, or flag what has changed since entry.

{symbol}: entry ${position.get('entry')}, now ${position.get('current')}, P&L {position.get('pl_pct')}%, held {position.get('days_held', '?')} days.
Current technicals: {tech}
Recent news:
{news_block}

Respond with two fields:
- "insight": a complete sentence (10-30 words) explaining what's happening and your reasoning — NOT a single word.
- "status": exactly one of "Valid" (setup intact, hold), "Caution" (something weakening — watch closely), or "Broken" (thesis no longer holds — consider exiting).

Example insight: "Still above its rising 50-day average with healthy volume, so the uptrend remains intact despite the recent pause." """

    result = await _provider_json(prompt, POSITION_SCHEMA)
    if not result:
        return {"insight": "AI insight unavailable.", "status": "Caution", "error": True}
    if result.get("status") not in ("Valid", "Caution", "Broken"):
        result["status"] = "Caution"
    result.setdefault("insight", "")
    return result


async def analyze_single(stock: dict) -> None:
    """On-demand analysis for one stock (user clicked Analyze)."""
    provider = _provider()
    if provider == "none":
        stock["ai"] = _fallback("AI analysis disabled (AI_PROVIDER=none).")
        return

    news = await asyncio.to_thread(_fetch_news, stock["ticker"])
    if provider == "anthropic":
        if not _anthropic_key():
            stock["ai"] = _fallback("AI_PROVIDER=anthropic but no ANTHROPIC_API_KEY set in .env.")
            return
        import anthropic

        stock["ai"] = await _anthropic_call(anthropic.AsyncAnthropic(), stock, news)
    else:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=5)) as client:
            error = await _ollama_preflight(client)
            if error:
                stock["ai"] = _fallback(error)
                return
            stock["ai"] = await _ollama_call(client, _ollama_model(), stock, news)
    stock.pop("ai_status", None)
