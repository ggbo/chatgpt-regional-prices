"""Normalize official ChatGPT checkout pricing configuration responses."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0.0"
COLLECTOR_VERSION = "1.0.0"
SOURCE_ROOT = "https://chatgpt.com/backend-api/checkout_pricing_config/configs"
KNOWN_TAX_VALUES = {"inclusive", "exclusive"}


def _currency_codes() -> set[str]:
    path = Path(__file__).resolve().parents[1] / "config" / "currencies.json"
    try:
        return set(json.loads(path.read_text(encoding="utf-8"))["currencies"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return set()


CURRENCY_CODES = _currency_codes()


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def decimal_text(value: Any) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("expected finite JSON number")
    return format(value, "f")


def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


def _metadata(config: dict[str, Any], key: str, issues: list[dict[str, Any]]) -> str | None:
    value = config.get(key)
    if value is None:
        return None
    try:
        result = decimal_text(value)
        if value < 0:
            raise ValueError("negative metadata value")
        if key == "minor_unit_exponent" and value != value.to_integral_value():
            raise ValueError("fractional minor unit exponent")
        return result
    except ValueError as exc:
        issues.append({"field": key, "metadata_error": str(exc)})
        return None


def _amount_variant(
    node: Any,
    pointer: str,
    observed_at: str,
    body_sha256: str,
    source_url: str,
) -> dict[str, Any]:
    if not isinstance(node, dict):
        raise ValueError("price node missing")
    amount = node.get("amount")
    amount_text = decimal_text(amount)
    if amount <= 0:
        raise ValueError("standard subscription amount must be positive")
    raw_tax = node.get("tax")
    return {
        "amount": amount_text,
        "tax": raw_tax if raw_tax in KNOWN_TAX_VALUES else "unknown",
        "source_tax_value": json_safe(raw_tax),
        "json_pointer": pointer,
        "last_success_at": observed_at,
        "price_changed_at": observed_at,
        "source": {"body_sha256": body_sha256, "url": source_url},
    }


def normalize_config(
    raw: bytes,
    target: dict[str, Any],
    plans: list[dict[str, Any]],
    observed_at: str,
) -> dict[str, Any]:
    """Parse one official response without guessing missing values.

    A structurally valid response returns one plan result for every requested plan.
    Individual malformed/missing plans therefore do not discard valid sibling plans.
    """

    doc = json.loads(
        raw.decode("utf-8"),
        parse_float=Decimal,
        parse_int=Decimal,
        parse_constant=_reject_constant,
    )
    if not isinstance(doc, dict):
        raise ValueError("config must be an object")

    expected = target.get("country_code") or target.get("config_id")
    actual = doc.get("country_code")
    if actual != expected:
        raise ValueError(f"country_mismatch: expected {expected}, got {actual}")
    config = doc.get("currency_config")
    if not isinstance(config, dict):
        raise ValueError("currency_config missing")
    currency = config.get("symbol_code")
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
        raise ValueError("currency code missing or malformed")
    currency = currency.upper()
    if CURRENCY_CODES and currency not in CURRENCY_CODES:
        raise ValueError(f"unsupported currency code: {currency}")

    body_sha256 = hashlib.sha256(raw).hexdigest()
    config_id = target["config_id"]
    source_url = f"{SOURCE_ROOT}/{config_id}"
    source = {
        "kind": "official_web_config",
        "url": source_url,
        "body_sha256": body_sha256,
        "collector_version": COLLECTOR_VERSION,
    }
    issues: list[dict[str, Any]] = []
    tax_percent = _metadata(config, "tax_percent", issues)
    exponent = _metadata(config, "minor_unit_exponent", issues)
    metadata_issues = [item.copy() for item in issues]
    quotes: list[dict[str, Any]] = []
    plan_results: list[dict[str, Any]] = []

    for plan_spec in plans:
        plan_id = plan_spec["plan_id"]
        source_key = plan_spec["source_plan_key"]
        plan = config.get(source_key)
        if not isinstance(plan, dict) or "month" not in plan:
            result = {"plan_id": plan_id, "status": "plan_field_missing", "reason": "month field missing"}
            plan_results.append(result)
            issues.append(result.copy())
            continue
        month = plan.get("month")
        pointer = f"/currency_config/{source_key}/month"
        try:
            display = _amount_variant(month, pointer, observed_at, body_sha256, source_url)
        except (ValueError, TypeError) as exc:
            result = {"plan_id": plan_id, "status": "amount_invalid", "reason": str(exc)}
            plan_results.append(result)
            issues.append(result.copy())
            continue

        quote_issues: list[dict[str, Any]] = [item.copy() for item in metadata_issues]
        psp = None
        psp_status = "not_present"
        if isinstance(month, dict) and "psp_override" in month:
            try:
                psp = _amount_variant(
                    month["psp_override"], pointer + "/psp_override", observed_at, body_sha256, source_url
                )
                psp_status = "valid"
            except (ValueError, TypeError) as exc:
                psp_status = "invalid"
                issue = {"plan_id": plan_id, "psp_error": str(exc)}
                quote_issues.append(issue)
                issues.append(issue.copy())

        quote = {
            "record_id": f"{config_id}:web:{plan_id}:month:standard",
            "target_id": target["target_id"],
            "country_code": target.get("country_code"),
            "config_id": config_id,
            "region_kind": target.get("region_kind", "unknown"),
            "channel": "web",
            "plan_id": plan_id,
            "source_plan_key": source_key,
            "billing_interval": "month",
            "offer_type": "standard",
            "currency": currency,
            "display_price": display,
            "psp_override": psp,
            "psp_override_status": psp_status,
            "tax_percent": tax_percent,
            "minor_unit_exponent": exponent,
            "source_metadata": {
                "tax_percent": json_safe(config.get("tax_percent")),
                "minor_unit_exponent": json_safe(config.get("minor_unit_exponent")),
            },
            "availability": {
                "configuration_present": True,
                "purchase_verified": False,
                "pricing_rollout_gate": json_safe(config.get("pricing_rollout_gate")),
            },
            "source": source.copy(),
            "estimates": {"USD": None, "CNY": None},
            "last_attempt_at": observed_at,
            "last_success_at": observed_at,
            "price_changed_at": observed_at,
            "issues": quote_issues,
        }
        quotes.append(quote)
        plan_results.append({"plan_id": plan_id, "status": "valid_quote", "reason": None})

    return {
        "quotes": quotes,
        "plan_results": plan_results,
        "issues": issues,
        "response_country_code": actual,
        "body_sha256": body_sha256,
    }
