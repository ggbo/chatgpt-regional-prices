"""Bounded, resumable collection of regional ChatGPT price configurations."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

try:
    from .pricing import SOURCE_ROOT, normalize_config
except ImportError:  # pragma: no cover - direct script execution
    from pricing import SOURCE_ROOT, normalize_config

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
RETRYABLE_HTTP = {429, 500, 502, 503, 504}


class _RequestPacer:
    """Reserve globally spaced request start times across collector threads."""

    def __init__(self, interval_seconds: float):
        if interval_seconds < 0:
            raise ValueError("min_request_interval_seconds must not be negative")
        self.interval_seconds = interval_seconds
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self, deadline: float) -> bool:
        if self.interval_seconds == 0:
            return time.monotonic() < deadline
        with self._lock:
            now = time.monotonic()
            scheduled = max(now, self._next_allowed)
            if scheduled >= deadline:
                return False
            self._next_allowed = scheduled + self.interval_seconds
        delay = scheduled - now
        while delay > 0:
            time.sleep(delay)
            delay = scheduled - time.monotonic()
        return time.monotonic() < deadline


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _save_evidence(raw: bytes, evidence_dir: Path) -> str:
    digest = hashlib.sha256(raw).hexdigest()
    path = evidence_dir / f"{digest}.json"
    if not path.exists():
        evidence_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    return digest


def _retry_delay(headers: Any, attempt: int) -> float:
    value = headers.get("Retry-After") if headers else None
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return min(8.0, 2.0**attempt)


def _request_target(
    target: dict[str, Any],
    plans: list[dict[str, Any]],
    deadline: float,
    opener: Callable[..., Any],
    pacer: _RequestPacer,
) -> dict[str, Any]:
    observed_at: str | None = None
    url = f"{SOURCE_ROOT}/{target['config_id']}"
    headers = {"Accept": "application/json", "User-Agent": "chatgpt-regional-prices/1.0"}
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            kind = "not_attempted" if attempt == 0 else "request_failed"
            return {"kind": kind, "reason": "collection budget exhausted", "observed_at": observed_at}
        if not pacer.wait(deadline):
            kind = "not_attempted" if attempt == 0 else "request_failed"
            return {"kind": kind, "reason": "request interval exceeds remaining collection budget", "observed_at": observed_at}
        observed_at = utc_now()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            kind = "not_attempted" if attempt == 0 else "request_failed"
            return {"kind": kind, "reason": "collection budget exhausted", "observed_at": observed_at if attempt else None}
        request = urllib.request.Request(url, headers=headers)
        try:
            with opener(request, timeout=min(15.0, remaining)) as response:
                status = getattr(response, "status", 200)
                content_type = response.headers.get_content_type() if hasattr(response.headers, "get_content_type") else response.headers.get("Content-Type", "")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    return {"kind": "request_failed", "http_status": status, "reason": "response exceeds size limit", "observed_at": observed_at}
                if status != 200:
                    raise urllib.error.HTTPError(url, status, "unexpected status", response.headers, None)
                if "json" not in str(content_type).lower():
                    return {"kind": "request_failed", "http_status": status, "reason": f"unexpected content type: {content_type}", "observed_at": observed_at}
                try:
                    normalized = normalize_config(raw, target, plans, observed_at)
                except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    kind = "country_mismatch" if str(exc).startswith("country_mismatch") else "request_failed"
                    return {
                        "kind": kind,
                        "http_status": status,
                        "reason": str(exc),
                        "structural_failure": str(exc) == "currency_config missing",
                        "raw": raw,
                        "observed_at": observed_at,
                    }
                return {"kind": "success", "http_status": status, "normalized": normalized, "raw": raw, "observed_at": observed_at}
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return {"kind": "access_blocked", "http_status": exc.code, "reason": f"HTTP {exc.code}", "observed_at": observed_at}
            if exc.code == 404:
                return {"kind": "request_failed", "http_status": 404, "reason": "configuration not found", "observed_at": observed_at}
            if exc.code not in RETRYABLE_HTTP or attempt == 2:
                return {"kind": "request_failed", "http_status": exc.code, "reason": f"HTTP {exc.code}", "observed_at": observed_at}
            delay = _retry_delay(exc.headers, attempt)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == 2:
                return {"kind": "request_failed", "reason": str(exc), "observed_at": observed_at}
            delay = min(8.0, 2.0**attempt)
        remaining = deadline - time.monotonic()
        if delay > 30:
            return {"kind": "request_failed", "reason": f"Retry-After {delay:g}s exceeds bounded retry cap", "observed_at": observed_at}
        if remaining <= delay:
            return {"kind": "request_failed", "reason": "retry budget exhausted", "observed_at": observed_at}
        time.sleep(delay)
    raise AssertionError("unreachable")


def _change_hash(record_id: str, variant: str, old: dict[str, Any], new: dict[str, Any], currency: str) -> str:
    data = {
        "record_id": record_id,
        "variant": variant,
        "old": {"amount": old.get("amount"), "tax": old.get("tax")},
        "new": {"amount": new.get("amount"), "tax": new.get("tax")},
        "currency": currency,
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _needs_review(old: dict[str, Any], new: dict[str, Any]) -> tuple[bool, str | None]:
    if old.get("tax") != new.get("tax"):
        return True, "tax_changed"
    try:
        before, after = Decimal(old["amount"]), Decimal(new["amount"])
        if before and abs(after - before) / before > Decimal("0.20"):
            return True, "price_change_over_20_percent"
    except (KeyError, InvalidOperation):
        return True, "amount_comparison_failed"
    return False, None


def _merge_quote(
    old: dict[str, Any] | None,
    new: dict[str, Any],
    accepted: set[str],
) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    if old is None:
        return new, [], False
    reviews: list[dict[str, Any]] = []
    source_hash = new.get("source", {}).get("body_sha256")
    currency_changed = old.get("currency") != new.get("currency")
    if currency_changed:
        fingerprint = hashlib.sha256(
            f"{new['record_id']}|currency|{old.get('currency')}|{new.get('currency')}".encode()
        ).hexdigest()
        if fingerprint not in accepted and source_hash not in accepted:
            kept = copy.deepcopy(old)
            kept["last_attempt_at"] = new["last_attempt_at"]
            review = {"change_hash": fingerprint, "record_id": new["record_id"], "variant": "all", "reason": "currency_changed", "candidate": new}
            kept.setdefault("issues", []).append({k: v for k, v in review.items() if k != "candidate"})
            return kept, [review], True
        return new, [], False

    display_review, reason = _needs_review(old["display_price"], new["display_price"])
    display_hash = _change_hash(new["record_id"], "display_price", old["display_price"], new["display_price"], new["currency"])
    if display_review and display_hash not in accepted and source_hash not in accepted:
        kept = copy.deepcopy(old)
        kept["last_attempt_at"] = new["last_attempt_at"]
        review = {"change_hash": display_hash, "record_id": new["record_id"], "variant": "display_price", "reason": reason, "candidate": new["display_price"]}
        kept.setdefault("issues", []).append({k: v for k, v in review.items() if k != "candidate"})
        kept.setdefault("availability", {})["last_attempt_configuration_present"] = True
        reviews.append(review)
        _merge_psp_variant(kept, old, new, accepted, reviews)
        return kept, reviews, True

    merged = copy.deepcopy(new)
    unchanged_display = all(old["display_price"].get(key) == new["display_price"].get(key) for key in ("amount", "tax")) and old.get("currency") == new.get("currency")
    if unchanged_display:
        merged["display_price"]["price_changed_at"] = old["display_price"].get("price_changed_at", old.get("price_changed_at"))
        merged["price_changed_at"] = old.get("price_changed_at")

    _merge_psp_variant(merged, old, new, accepted, reviews)
    return merged, reviews, False


def _merge_psp_variant(
    merged: dict[str, Any],
    old: dict[str, Any],
    new: dict[str, Any],
    accepted: set[str],
    reviews: list[dict[str, Any]],
) -> None:
    """Merge PSP independently so a display quarantine never discards valid PSP evidence."""

    old_psp = old.get("psp_override")
    new_psp = new.get("psp_override")
    new_status = new.get("psp_override_status")
    source_hash = new.get("source", {}).get("body_sha256")
    if new_status == "invalid":
        if old_psp:
            merged["psp_override"] = copy.deepcopy(old_psp)
            merged["psp_override_status"] = "stale"
        else:
            merged["psp_override"] = None
            merged["psp_override_status"] = "invalid"
        merged.setdefault("issues", []).extend(
            item for item in new.get("issues", []) if "psp_error" in item
        )
        return
    if new_status == "not_present":
        merged["psp_override"] = None
        merged["psp_override_status"] = "not_present"
        return
    if not new_psp:
        return
    if not old_psp:
        merged["psp_override"] = copy.deepcopy(new_psp)
        merged["psp_override_status"] = "valid"
        return
    needs_review, reason = _needs_review(old_psp, new_psp)
    fingerprint = _change_hash(new["record_id"], "psp_override", old_psp, new_psp, new["currency"])
    if needs_review and fingerprint not in accepted and source_hash not in accepted:
        merged["psp_override"] = copy.deepcopy(old_psp)
        merged["psp_override_status"] = "stale"
        review = {"change_hash": fingerprint, "record_id": new["record_id"], "variant": "psp_override", "reason": reason, "candidate": new_psp}
        merged.setdefault("issues", []).append({k: v for k, v in review.items() if k != "candidate"})
        reviews.append(review)
        return
    merged["psp_override"] = copy.deepcopy(new_psp)
    merged["psp_override_status"] = "valid"
    if all(old_psp.get(key) == new_psp.get(key) for key in ("amount", "tax")):
        merged["psp_override"]["price_changed_at"] = old_psp.get("price_changed_at")


def _cell(target: dict[str, Any], plan: dict[str, Any], **values: Any) -> dict[str, Any]:
    base = {
        "cell_id": f"{target['target_id']}:web:{plan['plan_id']}:month:standard",
        "target_id": target["target_id"],
        "country_code": target.get("country_code"),
        "config_id": target["config_id"],
        "region_kind": target.get("region_kind", "unknown"),
        "plan_id": plan["plan_id"],
        "channel": "web",
        "billing_interval": "month",
        "offer_type": "standard",
        "status": "not_attempted",
        "attempt_status": "not_attempted",
        "quote_record_id": None,
        "last_attempt_at": None,
        "last_success_at": None,
        "http_status": None,
        "reason": None,
    }
    base.update(values)
    return base


def _summary(regions: list[dict[str, Any]], cells: list[dict[str, Any]], successful: int) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for cell in cells:
        counts[cell["status"]] = counts.get(cell["status"], 0) + 1
    plans_by_target: dict[str, set[str]] = {}
    for cell in cells:
        if cell["status"] == "valid_quote":
            plans_by_target.setdefault(cell["target_id"], set()).add(cell["plan_id"])
    attempted_targets = {cell["target_id"] for cell in cells if cell["attempt_status"] != "not_attempted"}
    return {
        "target_count": len(regions),
        "country_target_count": sum(bool(item.get("is_country_directory")) for item in regions),
        "auxiliary_target_count": sum(not bool(item.get("is_country_directory")) for item in regions),
        "expected_request_count": len(regions),
        "attempted_request_count": len(attempted_targets),
        "not_attempted_request_count": len(regions) - len(attempted_targets),
        "successful_response_count": successful,
        "expected_cell_count": len(cells),
        "status_counts": counts,
        "current_quote_cell_count": counts.get("valid_quote", 0),
        "historical_quote_cell_count": counts.get("stale_quote", 0),
        "complete_five_plan_target_count": sum(len(value) == 5 for value in plans_by_target.values()),
    }


def collect(
    regions: list[dict[str, Any]],
    plans: list[dict[str, Any]],
    previous_quotes: list[dict[str, Any]],
    evidence_dir: str | Path,
    budget_seconds: float = 900,
    concurrency: int = 2,
    checkpoint_path: str | Path | None = None,
    accepted_change_hashes: set[str] | None = None,
    opener: Callable[..., Any] | None = None,
    now: Callable[[], str] | None = None,
    checkpoint_max_age_seconds: float = 72 * 3600,
    min_request_interval_seconds: float = 0,
    probe_first: bool = True,
    initial_fill: bool = False,
    initial_fill_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Collect the entire target matrix within a hard scheduling budget."""

    if concurrency < 1:
        raise ValueError("concurrency must be positive")
    started_at = (now or utc_now)()
    accepted = accepted_change_hashes or set()
    opener = opener or urllib.request.urlopen
    pacer = _RequestPacer(min_request_interval_seconds)
    started = time.monotonic()
    deadline = started + max(0.0, budget_seconds)
    evidence_path = Path(evidence_dir)
    checkpoint = Path(checkpoint_path) if checkpoint_path else None
    attempts: dict[str, dict[str, Any]] = {}
    initial_fill_attempt_counts = copy.deepcopy(initial_fill_counts or {})
    checkpoint_signature = hashlib.sha256(json.dumps({
        "targets": [(item["target_id"], item["config_id"]) for item in regions],
        "plans": [(item["plan_id"], item["source_plan_key"], item.get("billing_interval")) for item in plans],
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    checkpoint_created = time.time()
    if checkpoint and checkpoint.exists():
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        saved_created = saved.get("created_unix", checkpoint.stat().st_mtime)
        checkpoint_age = time.time() - saved_created
        expected_target_ids = [item["target_id"] for item in regions]
        expected_plan_keys = {item["plan_id"]: item["source_plan_key"] for item in plans}
        successful_saved = [item for item in saved.get("attempts", {}).values() if item.get("kind") == "success"]
        legacy_compatible = (
            saved.get("signature") is None
            and saved.get("target_ids") == expected_target_ids
            and bool(successful_saved)
            and all(
                all(
                    expected_plan_keys.get(quote.get("plan_id")) == quote.get("source_plan_key")
                    for quote in item.get("normalized", {}).get("quotes", [])
                )
                and {result.get("plan_id") for result in item.get("normalized", {}).get("plan_results", [])}
                == set(expected_plan_keys)
                for item in successful_saved
            )
        )
        if (saved.get("signature") == checkpoint_signature or legacy_compatible) and 0 <= checkpoint_age <= checkpoint_max_age_seconds:
            attempts = saved.get("attempts", {})
            saved_counts = saved.get("initial_fill_attempt_counts", {})
            if not isinstance(saved_counts, dict):
                raise ValueError('invalid initial fill attempt accounting in checkpoint')
            for key, count in saved_counts.items():
                initial_fill_attempt_counts[key] = max(initial_fill_attempt_counts.get(key, 0), count) if type(count) is int else count
            expected_ids = {item['target_id'] for item in regions}
            if not isinstance(initial_fill_attempt_counts, dict) or any(
                key not in expected_ids or type(value) is not int or not 0 <= value <= 10
                for key, value in initial_fill_attempt_counts.items()
            ):
                raise ValueError('invalid initial fill attempt accounting in checkpoint')
            checkpoint_created = saved_created
            if legacy_compatible:
                _atomic_json(checkpoint, {
                    "created_unix": checkpoint_created,
                    "signature": checkpoint_signature,
                    "attempts": attempts,
                    "initial_fill_attempt_counts": initial_fill_attempt_counts,
                })

    expected_ids = {item['target_id'] for item in regions}
    if any(key not in expected_ids or type(count) is not int or not 0 <= count <= 10 for key, count in initial_fill_attempt_counts.items()):
        raise ValueError('invalid initial fill attempt accounting')
    if initial_fill:
        for item in regions:
            key = item['target_id']
            if initial_fill_attempt_counts.get(key, 0) >= 10 and key not in attempts:
                attempts[key] = {'kind': 'not_attempted', 'observed_at': None, 'reason': 'initial fill attempt limit reached'}
    pending = [
        item for item in regions
        if (not initial_fill or initial_fill_attempt_counts.get(item['target_id'], 0) < 10) and (item["target_id"] not in attempts
        or (
            attempts[item["target_id"]].get("kind") == "not_attempted"
            and attempts[item["target_id"]].get("observed_at") is None
        ))
    ]
    probes = sorted((item for item in pending if probe_first and item["target_id"] in {"US", "PH"}), key=lambda item: item["target_id"] == "PH")
    probe_ids = {item["target_id"] for item in probes}
    # Scheduler order prevents budget-limited retries from starving the tail.
    remaining_targets = [item for item in pending if item["target_id"] not in probe_ids]

    def save_attempt(target: dict[str, Any], attempt: dict[str, Any]) -> None:
        raw = attempt.pop("raw", None)
        if raw is not None:
            attempt["evidence_sha256"] = _save_evidence(raw, evidence_path)
        if initial_fill and attempt.get('observed_at') and attempt['kind'] != 'not_attempted':
            key = target['target_id']
            initial_fill_attempt_counts[key] = min(10, initial_fill_attempt_counts.get(key, 0) + 1)
        attempts[target["target_id"]] = attempt
        if checkpoint:
            _atomic_json(checkpoint, {
                "created_unix": checkpoint_created,
                "signature": checkpoint_signature,
                "attempts": attempts,
                "initial_fill_attempt_counts": initial_fill_attempt_counts,
            })

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        def run_batch(targets: list[dict[str, Any]]) -> None:
            futures = {
                executor.submit(_request_target, target, plans, deadline, opener, pacer): target
                for target in targets if time.monotonic() < deadline
            }
            for future in as_completed(futures):
                target = futures[future]
                try:
                    attempt = future.result()
                except Exception as exc:  # isolate one target from the rest of the matrix
                    attempt = {"kind": "request_failed", "reason": f"collector worker failed: {exc}", "observed_at": utc_now()}
                save_attempt(target, attempt)

        run_batch(probes)
        probe_attempts = [attempts[item["target_id"]] for item in probes if item["target_id"] in attempts]
        shared_structural_failure = bool(probe_attempts) and len(probe_attempts) == len(probes) and all(
            item.get("structural_failure") for item in probe_attempts
        )
        if shared_structural_failure:
            for target in remaining_targets:
                attempts[target["target_id"]] = {
                    "kind": "not_attempted",
                    "reason": "shared structural probe failure: currency_config missing",
                    "observed_at": None,
                }
            if checkpoint:
                _atomic_json(checkpoint, {
                    "created_unix": checkpoint_created,
                    "signature": checkpoint_signature,
                    "attempts": attempts,
                    "initial_fill_attempt_counts": initial_fill_attempt_counts,
                })
        else:
            run_batch(remaining_targets)

    previous_by_id = {quote["record_id"]: quote for quote in previous_quotes}
    output_quotes: dict[str, dict[str, Any]] = {}
    cells: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    successful = 0
    for target in regions:
        attempt = attempts.get(target["target_id"], {"kind": "not_attempted", "reason": "collection budget exhausted", "observed_at": None})
        normalized = attempt.get("normalized")
        new_by_plan = {quote["plan_id"]: quote for quote in normalized.get("quotes", [])} if normalized else {}
        result_by_plan = {result["plan_id"]: result for result in normalized.get("plan_results", [])} if normalized else {}
        if attempt["kind"] == "success":
            successful += 1
        for plan in plans:
            record_id = f"{target['config_id']}:web:{plan['plan_id']}:month:standard"
            old = previous_by_id.get(record_id)
            if plan["plan_id"] in new_by_plan:
                merged, quote_reviews, quarantined = _merge_quote(old, new_by_plan[plan["plan_id"]], accepted)
                output_quotes[record_id] = merged
                reviews.extend(quote_reviews)
                status = "stale_quote" if quarantined else "valid_quote"
                cells.append(_cell(
                    target, plan, status=status, attempt_status="valid_quote",
                    quote_record_id=record_id, last_attempt_at=attempt.get("observed_at"),
                    last_success_at=merged.get("last_success_at"), http_status=attempt.get("http_status"),
                    reason=("change_requires_review: " + quote_reviews[0]["reason"]) if quarantined else None,
                ))
                continue
            attempt_status = result_by_plan.get(plan["plan_id"], {}).get("status", attempt["kind"])
            reason = result_by_plan.get(plan["plan_id"], {}).get("reason", attempt.get("reason"))
            if old:
                kept = copy.deepcopy(old)
                kept["last_attempt_at"] = attempt.get("observed_at")
                kept.setdefault("availability", {})["last_attempt_configuration_present"] = (
                    False if attempt_status == "plan_field_missing" else None
                )
                kept.setdefault("issues", []).append({"attempt_status": attempt_status, "reason": reason, "at": attempt.get("observed_at")})
                output_quotes[record_id] = kept
                cells.append(_cell(
                    target, plan, status="stale_quote", attempt_status=attempt_status, quote_record_id=record_id,
                    last_attempt_at=attempt.get("observed_at"), last_success_at=old.get("last_success_at"),
                    http_status=attempt.get("http_status"), reason=reason,
                ))
            else:
                cells.append(_cell(
                    target, plan, status=attempt_status, attempt_status=attempt_status,
                    last_attempt_at=attempt.get("observed_at"), http_status=attempt.get("http_status"), reason=reason,
                ))

    summary = _summary(regions, cells, successful)
    source_status = {
        "started_at": started_at,
        "completed_at": utc_now(),
        "all_source_failed": successful == 0,
        "structural_failure": any(a.get("structural_failure") for a in attempts.values()),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    return {
        'initial_fill_attempt_counts': initial_fill_attempt_counts,
        "quotes": sorted(output_quotes.values(), key=lambda item: item["record_id"]),
        "cells": cells,
        "summary": summary,
        "source_status": source_status,
        "change_reviews": reviews,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regions", default="config/regions.json")
    parser.add_argument("--plans", default="config/plans.json")
    parser.add_argument("--previous")
    parser.add_argument("--evidence-dir", default="work/evidence")
    parser.add_argument("--checkpoint", default="work/collector-checkpoint.json")
    parser.add_argument("--output", default="work/local-scan.json")
    parser.add_argument("--budget-seconds", type=float, default=900)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--min-request-interval-seconds", type=float, default=0)
    args = parser.parse_args()
    regions_doc = json.loads(Path(args.regions).read_text(encoding="utf-8"))
    plans_doc = json.loads(Path(args.plans).read_text(encoding="utf-8"))
    previous: list[dict[str, Any]] = []
    if args.previous and Path(args.previous).exists():
        previous = json.loads(Path(args.previous).read_text(encoding="utf-8")).get("quotes", [])
    result = collect(
        regions_doc["regions"], plans_doc["plans"], previous, args.evidence_dir,
        budget_seconds=args.budget_seconds, concurrency=args.concurrency, checkpoint_path=args.checkpoint,
        min_request_interval_seconds=args.min_request_interval_seconds,
    )
    _atomic_json(Path(args.output), result)
    print(json.dumps(result["summary"], ensure_ascii=False))
    return 0 if not result["source_status"]["all_source_failed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
