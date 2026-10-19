"""Fetch one coherent Frankfurter FX snapshot and apply precise estimates."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import urllib.request
import urllib.parse
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Callable

DEFAULT_URL = "https://api.frankfurter.dev/v2/rates?base=USD&expand=providers"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_evidence(raw: bytes, evidence_dir: Path) -> str:
    digest = hashlib.sha256(raw).hexdigest()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = evidence_dir / f"{digest}.json"
    if not path.exists():
        path.write_bytes(raw)
    return digest


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _stale(previous: dict[str, Any] | None, generated_at: str, reason: str, url: str = DEFAULT_URL) -> dict[str, Any]:
    if not previous:
        return {
            "schema_version": "1.0.0", "fx_snapshot_id": None, "generated_at": generated_at,
            "fetched_at": None, "base": "USD", "stale": True,
            "source": {"kind": "frankfurter", "url": url, "error": reason}, "rates": [],
        }
    retained = copy.deepcopy(previous)
    retained["generated_at"] = generated_at
    retained["stale"] = True
    retained.setdefault("source", {})["refresh_error"] = reason
    return retained


def fetch_fx(
    previous: dict[str, Any] | None,
    evidence_dir: str | Path,
    timeout: float = 15,
    url: str = DEFAULT_URL,
    opener: Callable[..., Any] | None = None,
    now: Callable[[], str] | None = None,
    currencies: set[str] | list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    generated_at = (now or _now)()
    opener = opener or urllib.request.urlopen
    if currencies and url == DEFAULT_URL:
        requested = sorted({item for item in currencies if item != "USD"})
        query = urllib.parse.urlencode({"base": "USD", "quotes": ",".join(requested), "expand": "providers"})
        url = f"https://api.frankfurter.dev/v2/rates?{query}"
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "chatgpt-regional-prices/1.0"})
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("FX response exceeds size limit")
        doc = json.loads(raw, parse_float=Decimal, parse_int=Decimal, parse_constant=_reject_constant)
        rows = doc.get("rates") if isinstance(doc, dict) else doc
        if not isinstance(rows, list):
            raise ValueError("FX response must be an array")
        rates: list[dict[str, Any]] = [{"quote": "USD", "rate": "1", "rate_date": generated_at[:10], "providers": []}]
        seen = {"USD"}
        for row in rows:
            if not isinstance(row, dict) or row.get("base") != "USD":
                continue
            quote = row.get("quote")
            rate = row.get("rate")
            rate_date = row.get("date")
            try:
                date.fromisoformat(rate_date)
            except (TypeError, ValueError):
                continue
            if not isinstance(quote, str) or len(quote) != 3 or quote != quote.upper() or not isinstance(rate, Decimal) or not rate.is_finite() or rate <= 0:
                continue
            if quote in seen:
                raise ValueError(f"duplicate FX quote: {quote}")
            providers = row.get("providers", [])
            if not isinstance(providers, list):
                providers = []
            rates.append({
                "quote": quote, "rate": format(rate, "f"), "rate_date": rate_date,
                "providers": _json_safe(providers),
            })
            seen.add(quote)
        if len(rates) == 1:
            raise ValueError("FX response contained no usable rates")
        evidence_sha = _write_evidence(raw, Path(evidence_dir))
        canonical = json.dumps(rates, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        snapshot_id = hashlib.sha256(canonical.encode()).hexdigest()
        return {
            "schema_version": "1.0.0", "fx_snapshot_id": snapshot_id, "generated_at": generated_at,
            "fetched_at": generated_at, "base": "USD", "stale": False,
            "source": {"kind": "frankfurter", "url": url, "body_sha256": evidence_sha}, "rates": rates,
        }
    except Exception as exc:
        return _stale(previous, generated_at, str(exc), url)


def apply_estimates(quotes: list[dict[str, Any]], fx: dict[str, Any]) -> list[dict[str, Any]]:
    by_quote: dict[str, Decimal] = {}
    for item in fx.get("rates", []):
        try:
            rate = Decimal(item["rate"])
            if rate.is_finite() and rate > 0:
                by_quote[item["quote"]] = rate
        except (KeyError, InvalidOperation):
            continue
    result = copy.deepcopy(quotes)
    snapshot_id = fx.get("fx_snapshot_id")
    for quote in result:
        estimates = {"USD": None, "CNY": None}
        try:
            local_rate = by_quote[quote["currency"]]
            amount = Decimal(quote["display_price"]["amount"])
            with localcontext() as context:
                context.prec = 50
                usd = amount / local_rate
                estimates["USD"] = {"amount": format(usd, "f"), "fx_snapshot_id": snapshot_id}
                if "CNY" in by_quote:
                    cny = usd * by_quote["CNY"]
                    estimates["CNY"] = {"amount": format(cny, "f"), "fx_snapshot_id": snapshot_id}
        except (KeyError, InvalidOperation, ZeroDivisionError):
            pass
        quote["estimates"] = estimates
    return result
