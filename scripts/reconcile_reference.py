"""Reconcile the dated PriceAI identity baseline with collector coverage.

This module compares identities only.  It never imports third-party prices.
The ``import-priceai`` command exists to regenerate the checked-in baseline
from locally fetched PriceAI detail-page HTML while preserving provenance.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any


SCHEMA_VERSION = "1.0.0"

PRICEAI_PLANS = {
    "go": {
        "slug": "chatgpt__go-monthly",
        "key_prefix": "chatgpt-go-monthly",
        "reported_region_count": 170,
        "reported_quote_count": 235,
    },
    "plus": {
        "slug": "chatgpt__plus-monthly",
        "key_prefix": "chatgpt-plus-monthly",
        "reported_region_count": 69,
        "reported_quote_count": 106,
    },
    "pro_100": {
        "slug": "chatgpt__pro-5x",
        "key_prefix": "chatgpt-pro-5x",
        "reported_region_count": 64,
        "reported_quote_count": 98,
    },
    "pro_200": {
        "slug": "chatgpt__pro-20x",
        "key_prefix": "chatgpt-pro-20x",
        "reported_region_count": 70,
        "reported_quote_count": 146,
    },
    "pro_500": {
        "slug": "chatgpt__pro-500",
        "key_prefix": "chatgpt-pro-500",
        "reported_region_count": 77,
        "reported_quote_count": 112,
    },
}

CHANNELS = {
    "web_direct": "web",
    "app_store": "ios",
    "google_play": "google_play",
}

# Code mapping of the English names rendered by the official OpenAI Help Center
# article on 2026-10-19.  The source is a positive support list, not a price or
# purchase-availability list.
OPENAI_SUPPORTED_CODES = frozenset(
    """
    AL DZ AF AX AD AO AG AR AM AW AU AT AZ BS BH BD BB BE BZ BM BJ BT BO BA BW
    BR BN BG BF BI CV KH CM CA KY CF TD CL CO KM CG CD CR CI HR CY CZ DK DJ DM
    DO EC EG SV GQ ER EE SZ ET FO FJ FI FR GF PF TF GA GM GE DE GH GR GD GL GT
    GP GN GW GY HT VA HN HU IS IN ID IQ IE IL IT JM JP JO KZ KE KI KW KG LA LV
    LB LS LR LY LI LT LU MG MW MY MV ML MT MH MQ MR MU YT MX FM MD MC MN ME MA
    MZ MM NA NR NP NL NC NZ NI NE NG MK NO OM PK PW PS PA PG PY PE PH PL PT QA
    RE RO RW BL SH KN LC MF PM VC WS SM ST SA SN RS SC SL SG SK SI SB SO ZA KR
    SS ES LK SR SE CH SD SJ TW TJ TZ TH TL TG TO TT TN TR TM TV UG UA AE GB US
    UY UZ VU VN WF YE ZM ZW
    """.split()
)

OPENAI_SUPPORT_SOURCE = (
    "https://help.openai.com/en/articles/7947663-chatgpt-supported-countries"
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path: str | Path, value: Any) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _identity_source(channel: str, target_id: str) -> tuple[str, str]:
    if channel == "web":
        return (
            f"https://chatgpt.com/backend-api/checkout_pricing_config/configs/{target_id}",
            "derived_from_reference_target_and_official_config_path",
        )
    if channel == "ios":
        return (
            f"https://apps.apple.com/{target_id.lower()}/app/chatgpt/id6448311069",
            "derived_from_reference_target_and_official_app_id",
        )
    return (
        "https://play.google.com/store/apps/details?id=com.openai.chatgpt"
        f"&gl={target_id}",
        "derived_from_reference_target_and_official_app_id",
    )


def extract_priceai_html(
    *, plan_id: str, html_path: str | Path, catalog_target_ids: set[str]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Extract reference identities from Next.js row keys, without prices."""
    if plan_id not in PRICEAI_PLANS:
        raise ValueError(f"unsupported PriceAI plan: {plan_id}")
    spec = PRICEAI_PLANS[plan_id]
    path = Path(html_path)
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    pattern = re.compile(
        rf"{re.escape(spec['key_prefix'])}-(web_direct|app_store|google_play)-"
        r"([A-Z0-9]{2,3})"
    )
    observed = sorted(set(pattern.findall(text)), key=lambda item: (item[1], item[0]))
    source_page_url = f"https://priceai.cc/official-prices/{spec['slug']}"
    entries = []
    for raw_channel, target_id in observed:
        channel = CHANNELS[raw_channel]
        source_url, link_method = _identity_source(channel, target_id)
        entries.append(
            {
                "reference_id": f"{target_id}:{channel}:{plan_id}:month",
                "target_id": target_id,
                "country_code": target_id if len(target_id) == 2 and target_id != "EU" else None,
                "plan_id": plan_id,
                "billing_interval": "month",
                "channel": channel,
                "reference_status": (
                    "unverified_range" if channel == "google_play" else "listed"
                ),
                "source_url": source_url,
                "source_link_method": link_method,
                "source_page_url": source_page_url,
                "catalog_match": (
                    "matched" if target_id in catalog_target_ids else "missing_target_id"
                ),
            }
        )
    channel_counts = dict(sorted(Counter(entry["channel"] for entry in entries).items()))
    extracted_regions = len({entry["target_id"] for entry in entries})
    complete = (
        len(entries) == spec["reported_quote_count"]
        and extracted_regions == spec["reported_region_count"]
    )
    provenance = {
        "plan_id": plan_id,
        "source_page_url": source_page_url,
        "source_representation_url": source_page_url,
        "source_updated_date": "2026-10-13",
        "retrieved_date": "2026-10-19",
        "artifact_kind": "rendered_html_response",
        "artifact_sha256": hashlib.sha256(raw).hexdigest(),
        "reported_region_count": spec["reported_region_count"],
        "reported_quote_count": spec["reported_quote_count"],
        "extracted_region_count": extracted_regions,
        "extracted_identity_count": len(entries),
        "channel_counts": channel_counts,
        "completeness": "complete" if complete else "incomplete",
    }
    return entries, provenance


def build_priceai_baseline(
    regions: dict[str, Any], source_files: dict[str, str | Path]
) -> dict[str, Any]:
    target_ids = {region["target_id"] for region in regions.get("regions", [])}
    all_entries: list[dict[str, Any]] = []
    sources = []
    for plan_id in PRICEAI_PLANS:
        if plan_id not in source_files:
            raise ValueError(f"missing source file for {plan_id}")
        entries, source = extract_priceai_html(
            plan_id=plan_id,
            html_path=source_files[plan_id],
            catalog_target_ids=target_ids,
        )
        all_entries.extend(entries)
        sources.append(source)
    duplicate_ids = [
        key for key, count in Counter(row["reference_id"] for row in all_entries).items() if count > 1
    ]
    if duplicate_ids:
        raise ValueError(f"duplicate reference identities: {duplicate_ids[:5]}")
    unmapped = [row for row in all_entries if row["catalog_match"] != "matched"]
    complete = all(source["completeness"] == "complete" for source in sources) and not unmapped
    return {
        "schema_version": SCHEMA_VERSION,
        "baseline_id": "priceai-2026-10-13-extracted-2026-10-19",
        "checked_date": "2026-10-19",
        "status": "complete" if complete else "incomplete",
        "scope": "Reference identities only; no third-party price amounts are imported.",
        "sources": sources,
        "summary": {
            "identity_count": len(all_entries),
            "unique_region_count": len({row["target_id"] for row in all_entries}),
            "channel_counts": dict(
                sorted(Counter(row["channel"] for row in all_entries).items())
            ),
            "plan_counts": dict(
                sorted(Counter(row["plan_id"] for row in all_entries).items())
            ),
            "unmapped_identity_count": len(unmapped),
        },
        "entries": sorted(
            all_entries,
            key=lambda row: (row["plan_id"], row["target_id"], row["channel"]),
        ),
        "unmapped": unmapped,
        "notes": [
            "Google Play identities are retained as unverified_range because the reference page labels them as inferred.",
            "Web and store source URLs are deterministic links from the reference target ID and official app/config identifiers; source_link_method records this derivation.",
            "Counts describe the dated reference pages, not an official coverage ceiling or this project's completion target.",
        ],
    }


def build_support_status(regions: dict[str, Any]) -> dict[str, Any]:
    """Map the official positive support list onto the versioned catalog."""
    entries = []
    for region in regions.get("regions", []):
        code = region.get("country_code")
        if code in OPENAI_SUPPORTED_CODES:
            status = "supported"
            match = "official_english_name_mapped_to_iso_alpha_2"
        elif region.get("region_kind") == "iso_3166_1" and code:
            status = "unsupported"
            match = "not_listed_in_official_positive_support_list"
        else:
            status = "unknown"
            match = "non_iso_or_auxiliary_target_not_resolved_by_article"
        entries.append(
            {
                "target_id": region["target_id"],
                "country_code": code,
                "name_en": region.get("name_en"),
                "supported_status": status,
                "supported_checked_at": "2026-10-19",
                "source_url": OPENAI_SUPPORT_SOURCE,
                "match_method": match,
            }
        )
    counts = Counter(entry["supported_status"] for entry in entries)
    mapped_codes = {
        entry["country_code"]
        for entry in entries
        if entry["supported_status"] == "supported"
    }
    missing_codes = sorted(OPENAI_SUPPORTED_CODES - mapped_codes)
    status = "complete" if not missing_codes else "incomplete"
    return {
        "schema_version": SCHEMA_VERSION,
        "catalog_version": regions.get("catalog_version"),
        "checked_date": "2026-10-19",
        "status": status,
        "source": {
            "url": OPENAI_SUPPORT_SOURCE,
            "title": "ChatGPT Supported Countries",
            "rendered_updated_label": "2 months ago",
            "retrieved_date": "2026-10-19",
            "scope": "Supported countries and regions on web and mobile",
        },
        "summary": {
            "catalog_target_count": len(entries),
            "supported_count": counts["supported"],
            "unsupported_count": counts["unsupported"],
            "unknown_count": counts["unknown"],
            "official_codes_missing_from_catalog": missing_codes,
        },
        "entries": entries,
        "notes": [
            "Support status is independent of pricing-config retrieval and account purchase availability.",
            "unsupported means an ISO catalog entry was absent from the official positive list on the checked date.",
            "Non-ISO and auxiliary targets remain unknown unless the article identifies them directly.",
        ],
    }


def reconcile(
    regions: dict[str, Any],
    plans: dict[str, Any],
    cells: list[dict[str, Any]],
    baseline: dict[str, Any],
) -> dict[str, Any]:
    """Compare web reference identities with one collector coverage matrix."""
    region_by_id = {region["target_id"]: region for region in regions.get("regions", [])}
    plan_ids = {plan["plan_id"] for plan in plans.get("plans", [])}
    cell_by_key = {
        (
            cell.get("target_id"),
            cell.get("channel"),
            cell.get("plan_id"),
            cell.get("billing_interval"),
        ): cell
        for cell in cells
    }
    # Public comparison follows the site's official web-only product scope.
    # The dated source baseline can retain other channel identities for audit.
    entries = [entry for entry in baseline.get("entries", baseline.get("identities", []))
               if entry.get("channel") == "web"]
    comparisons: list[dict[str, Any]] = []
    reference_web_keys: set[tuple[Any, ...]] = set()

    for entry in entries:
        required = ("target_id", "channel", "plan_id", "billing_interval")
        missing = [field for field in required if not entry.get(field)]
        base = {
            "reference_id": entry.get("reference_id"),
            "target_id": entry.get("target_id"),
            "plan_id": entry.get("plan_id"),
            "channel": entry.get("channel"),
            "billing_interval": entry.get("billing_interval"),
            "reference_status": entry.get("reference_status", "listed"),
            "source_page_url": entry.get("source_page_url"),
        }
        flags = []
        region = region_by_id.get(entry.get("target_id"))
        if region and region.get("region_kind") != "iso_3166_1":
            flags.append("special_config")
        if missing or entry.get("plan_id") not in plan_ids:
            comparisons.append(
                {**base, "comparison": "source_field_missing", "flags": flags, "missing_fields": missing}
            )
            continue
        if not region:
            comparisons.append({**base, "comparison": "catalog_missing", "flags": flags})
            continue
        if entry["reference_status"] == "unverified_range":
            comparisons.append({**base, "comparison": "reference_inferred", "flags": flags})
            continue
        if entry["channel"] != "web":
            comparisons.append(
                {**base, "comparison": "reference_channel_out_of_scope", "flags": flags}
            )
            continue

        key = (entry["target_id"], "web", entry["plan_id"], entry["billing_interval"])
        reference_web_keys.add(key)
        cell = cell_by_key.get(key)
        if not cell:
            comparison = "collector_cell_missing"
            collector_status = None
        else:
            collector_status = cell.get("status")
            if collector_status == "valid_quote":
                comparison = "both_have"
            elif collector_status == "stale_quote":
                comparison = "both_have_historical"
            elif collector_status == "plan_field_missing":
                comparison = "reference_has_source_field_missing"
            elif collector_status in {
                "request_failed",
                "access_blocked",
                "country_mismatch",
                "amount_invalid",
            }:
                comparison = "reference_has_but_request_failed"
            elif collector_status == "not_attempted":
                comparison = "reference_not_attempted"
            elif collector_status == "period_unverified":
                comparison = "period_unverified"
            elif collector_status == "unverified_range":
                comparison = "reference_inferred"
            else:
                comparison = "collector_status_unknown"
        comparisons.append(
            {**base, "comparison": comparison, "flags": flags, "collector_status": collector_status}
        )

    for key, cell in cell_by_key.items():
        if (
            key not in reference_web_keys
            and key[1] == "web"
            and key[3] == "month"
            and cell.get("status") in {"valid_quote", "stale_quote"}
        ):
            region = region_by_id.get(cell.get("target_id"), {})
            comparisons.append(
                {
                    "reference_id": None,
                    "target_id": cell.get("target_id"),
                    "plan_id": cell.get("plan_id"),
                    "channel": "web",
                    "billing_interval": "month",
                    "reference_status": None,
                    "source_page_url": None,
                    "comparison": "official_new_discovery",
                    "flags": (
                        ["special_config"]
                        if region.get("region_kind") != "iso_3166_1"
                        else []
                    ),
                    "collector_status": cell.get("status"),
                }
            )

    counts = Counter(item["comparison"] for item in comparisons)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utcnow(),
        "catalog_version": regions.get("catalog_version"),
        "baseline_id": baseline.get("baseline_id"),
        "baseline_status": baseline.get("status", "not_verified"),
        "summary": {
            "reference_identity_count": len(entries),
            "reference_web_identity_count": sum(
                1 for entry in entries if entry.get("channel") == "web"
            ),
            "collector_cell_count": len(cells),
            "comparison_counts": dict(sorted(counts.items())),
            "special_config_count": sum(
                "special_config" in item.get("flags", []) for item in comparisons
            ),
        },
        "comparisons": sorted(
            comparisons,
            key=lambda item: (
                str(item.get("plan_id")),
                str(item.get("target_id")),
                str(item.get("channel")),
                str(item.get("comparison")),
            ),
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    importer = subparsers.add_parser("import-priceai")
    importer.add_argument("--regions", required=True)
    for plan_id in PRICEAI_PLANS:
        importer.add_argument(f"--{plan_id.replace('_', '-')}", required=True)
    importer.add_argument("--output", required=True)

    comparer = subparsers.add_parser("compare")
    comparer.add_argument("--regions", required=True)
    comparer.add_argument("--plans", required=True)
    comparer.add_argument("--coverage", required=True)
    comparer.add_argument("--baseline", required=True)
    comparer.add_argument("--output", required=True)

    support = subparsers.add_parser("build-support-status")
    support.add_argument("--regions", required=True)
    support.add_argument("--output", required=True)

    args = parser.parse_args()
    if args.command == "import-priceai":
        source_files = {
            plan_id: getattr(args, plan_id) for plan_id in PRICEAI_PLANS
        }
        baseline = build_priceai_baseline(_read(args.regions), source_files)
        _write(args.output, baseline)
        print(json.dumps(baseline["summary"], ensure_ascii=False))
        return 0 if baseline["status"] == "complete" else 2

    if args.command == "build-support-status":
        result = build_support_status(_read(args.regions))
        _write(args.output, result)
        print(json.dumps(result["summary"], ensure_ascii=False))
        return 0 if result["status"] == "complete" else 2

    coverage = _read(args.coverage)
    cells = coverage.get("cells", coverage) if isinstance(coverage, dict) else coverage
    report = reconcile(
        _read(args.regions), _read(args.plans), cells, _read(args.baseline)
    )
    _write(args.output, report)
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
