"""AI analysis: fetches recent news per ticker and asks a model for a
swing-trade read (summary, sentiment, risks/catalysts, confidence).

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

log = logging.getLogger(__name__)

ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_OLLAMA_MODEL = "llama3.2:3b"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
MAX_CONCURRENT_REQUESTS = 4  # anthropic only; ollama runs sequentially
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


def _build_prompt(stock: dict, news: list[dict]) -> str:
    if news:
        news_block = "\n".join(
            f"- [{n['source'] or 'unknown'}] {n['title']}"
            + (f" — {n['summary'][:300]}" if n["summary"] else "")
            for n in news
        )
    else:
        news_block = "(no recent news found for this ticker)"

    return f"""You are assisting a swing trader who holds positions for 2-5 days. The stock below just passed a technical scan (uptrend pullback: price above 50 SMA, 20 SMA above 50 SMA, RSI(14) below 50).

Ticker: {stock['ticker']}
Current price: ${stock['price']}
Relative strength rank: {stock['rs_rating']}/100 (vs the scanned universe)
Distance from 52-week high: {stock['pct_from_high']}% below
RSI(14): {stock['rsi']} (pulled back)
ADX(14): {stock['adx']} (trend strength)
ATR%: {stock['atr_pct']}% (daily volatility)
Relative volume: {stock['rel_volume']}x its 21-day average
20-day SMA: ${stock['sma20']}
50-day SMA: ${stock['sma50']} (price is {stock['pct_above_sma50']}% above it)
200-day SMA: ${stock['sma200']}
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


# ---------------------------------------------------------------- ollama (free, local)


def _ollama_url() -> str:
    return os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL).rstrip("/")


def _ollama_model() -> str:
    return os.environ.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)


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
    # "llama3.1:8b" should match an installed "llama3.1:8b" or "llama3.1:latest" base
    base = model.split(":")[0]
    if not any(name == model or name.split(":")[0] == base for name in installed):
        return f"Model '{model}' not found in Ollama. Run: ollama pull {model}"
    return None


async def _analyze_ollama(stocks: list[dict], progress) -> None:
    model = _ollama_model()
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=5)) as client:
        error = await _ollama_preflight(client)
        if error:
            log.warning("Ollama unavailable: %s", error)
            for stock in stocks:
                stock["ai"] = _fallback(error)
            return

        # Local inference: run sequentially — parallel requests just thrash the GPU.
        for i, stock in enumerate(stocks, 1):
            progress(f"Analyzing with {model} (local)… {i}/{len(stocks)}: {stock['ticker']}")
            news = await asyncio.to_thread(_fetch_news, stock["ticker"])
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
                stock["ai"] = _normalize(analysis)
            except Exception:
                log.exception("Ollama analysis failed for %s", stock["ticker"])
                stock["ai"] = _fallback("Local AI analysis failed — see backend logs.")


def _normalize(analysis: dict) -> dict:
    """Small local models occasionally drift outside the enums — clamp them."""
    if analysis.get("sentiment") not in ("Bullish", "Neutral", "Bearish"):
        analysis["sentiment"] = "Neutral"
    if analysis.get("confidence") not in ("High", "Medium", "Low"):
        analysis["confidence"] = "Low"
    analysis.setdefault("summary", "")
    analysis.setdefault("risks_catalysts", "")
    return analysis


# ---------------------------------------------------------------- anthropic (paid, optional)


async def _analyze_one_anthropic(client, semaphore: asyncio.Semaphore, stock: dict) -> None:
    import anthropic

    async with semaphore:
        news = await asyncio.to_thread(_fetch_news, stock["ticker"])
        try:
            response = await client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=1024,
                output_config={"format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA}},
                messages=[{"role": "user", "content": _build_prompt(stock, news)}],
            )
            text = next(b.text for b in response.content if b.type == "text")
            stock["ai"] = json.loads(text)
            stock["ai"]["news_count"] = len(news)
        except anthropic.APIError as e:
            log.error("Claude analysis failed for %s: %s", stock["ticker"], e)
            stock["ai"] = _fallback(f"AI analysis failed ({e.__class__.__name__}). Check your API key and credits.")
        except Exception:
            log.exception("Unexpected AI failure for %s", stock["ticker"])
            stock["ai"] = _fallback("AI analysis failed unexpectedly — see backend logs.")


async def _analyze_anthropic(stocks: list[dict], progress) -> None:
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or api_key == "sk-ant-...":  # unset or untouched .env.example placeholder
        for stock in stocks:
            stock["ai"] = _fallback("AI_PROVIDER=anthropic but no ANTHROPIC_API_KEY set in .env.")
        return

    client = anthropic.AsyncAnthropic()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    progress(f"Analyzing {len(stocks)} stocks with Claude…")
    await asyncio.gather(*(_analyze_one_anthropic(client, semaphore, s) for s in stocks))


# ---------------------------------------------------------------- entry point


async def analyze_all(stocks: list[dict], progress=lambda msg: None) -> None:
    """Mutates each stock dict in place, adding an 'ai' key."""
    if not stocks:
        return

    provider = os.environ.get("AI_PROVIDER", "ollama").strip().lower()
    if provider == "none":
        for stock in stocks:
            stock["ai"] = _fallback("AI analysis disabled (AI_PROVIDER=none).")
    elif provider == "anthropic":
        await _analyze_anthropic(stocks, progress)
    else:
        await _analyze_ollama(stocks, progress)
