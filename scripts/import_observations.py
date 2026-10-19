"""Validate and serially import offline official observations through the normal pipeline."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    from pricing import normalize_config
except ModuleNotFoundError:  # imported as scripts.import_observations in tests
    from scripts.pricing import normalize_config


ROOT = Path(__file__).resolve().parents[1]
FAILURE_KINDS = {"access_blocked", "request_failed", "country_mismatch"}
FAILURE_FIELDS = {"kind", "http_status", "reason", "observed_at"}
ALL_FAILED = "all_selected_sources_failed: no accepted current quote; valid published prices are preserved"
PAGE_READER_KIND = "official_page_reader"
PAGE_READER_REPRESENTATION = "extracted_json_text"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_import_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("import path must be a non-empty POSIX relative path")
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root.resolve()):
        raise ValueError("import path escaped import directory")
    return path


def valid_observed_at(value) -> str:
    if not isinstance(value, str):
        raise ValueError("observation time must be an ISO string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("observation time must include a timezone")
    return value


def checkpoint_signature(targets, plans) -> str:
    payload = {
        "targets": [(item["target_id"], item["config_id"]) for item in targets],
        "plans": [(item["plan_id"], item["source_plan_key"], item.get("billing_interval")) for item in plans],
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())


def page_reader_metadata(receipt_sha: str) -> dict:
    return {
        "retrieval_method": PAGE_READER_KIND,
        "evidence_representation": PAGE_READER_REPRESENTATION,
        "http_byte_exact": False,
        "capture_receipt_sha256": receipt_sha,
    }


def annotate_page_reader(normalized: dict, receipt_sha: str) -> dict:
    annotated = copy.deepcopy(normalized)
    metadata = page_reader_metadata(receipt_sha)
    for quote in annotated["quotes"]:
        quote["source"].update(metadata)
        quote["display_price"]["source"].update(metadata)
        psp = quote.get("psp_override")
        if isinstance(psp, dict) and isinstance(psp.get("source"), dict):
            psp["source"].update(metadata)
    return annotated


def validate_page_reader_receipt(raw: bytes, receipt: dict, receipt_sha: str,
                                 evidence_sha: str, observed_at: str, source_url: str):
    expected = {
        "kind": PAGE_READER_KIND,
        "captured_at": observed_at,
        "source_url": source_url,
        "body_sha256": evidence_sha,
        "http_status": None,
        "http_byte_exact": False,
        "representation": PAGE_READER_REPRESENTATION,
    }
    if not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in expected.items()):
        raise ValueError("page reader capture receipt metadata mismatch")
    reader_output = receipt.get("reader_output")
    body_text = raw.decode("utf-8")
    if (not isinstance(reader_output, str) or source_url not in reader_output
            or body_text not in reader_output):
        raise ValueError("page reader capture receipt output mismatch")
    if not re.fullmatch(r"[0-9a-f]{64}", receipt_sha):
        raise ValueError("page reader capture receipt SHA is invalid")


@dataclass
class ImportBatch:
    batch_id: str
    target_ids: list[str]
    checkpoint: dict
    evidence: dict[str, Path]


def validate_imports(import_dir: Path, regions_doc=None, plans_doc=None) -> list[ImportBatch]:
    import_dir = import_dir.resolve()
    spec = read_json(safe_import_path(import_dir, "imports/spec.json"))
    if not isinstance(spec, dict) or not re.fullmatch(r"[A-Za-z0-9._-]+", str(spec.get("batch_id", ""))):
        raise ValueError("invalid import batch_id")
    entries = spec.get("checkpoints")
    if not isinstance(entries, list) or not entries:
        raise ValueError("import spec must contain checkpoints")
    regions_doc = regions_doc or read_json(ROOT / "config/regions.json")
    plans_doc = plans_doc or read_json(ROOT / "config/plans.json")
    regions = {item["target_id"]: item for item in regions_doc["regions"]}
    plans = plans_doc["plans"]
    if len(plans) != 5:
        raise ValueError("import requires the five configured monthly plans")
    batches = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "target_ids"}:
            raise ValueError("checkpoint spec fields are invalid")
        target_ids = entry["target_ids"]
        if (not isinstance(target_ids, list) or not target_ids
                or len(target_ids) != len(set(target_ids))
                or any(key not in regions for key in target_ids)):
            raise ValueError("checkpoint target_ids are invalid")
        source = safe_import_path(import_dir, entry["path"])
        raw_checkpoint = source.read_bytes()
        if not re.fullmatch(r"[0-9a-f]{64}", str(entry["sha256"])) or sha256(raw_checkpoint) != entry["sha256"]:
            raise ValueError("checkpoint SHA256 mismatch")
        supplied = json.loads(raw_checkpoint.decode("utf-8"))
        attempts = supplied.get("attempts")
        if not isinstance(attempts, dict) or not set(target_ids).issubset(attempts):
            raise ValueError("checkpoint attempts do not contain every target_id")
        extras = set(attempts) - set(target_ids)
        if any(
            not isinstance(attempts[key], dict)
            or attempts[key].get("kind") != "not_attempted"
            or attempts[key].get("observed_at") is not None
            for key in extras
        ):
            raise ValueError("checkpoint contains an observed attempt outside target_ids")
        evidence = {}
        clean_attempts = {}
        for target_id in target_ids:
            attempt = attempts[target_id]
            if not isinstance(attempt, dict):
                raise ValueError(f"invalid attempt for {target_id}")
            observed_at = valid_observed_at(attempt.get("observed_at"))
            kind = attempt.get("kind")
            if kind == "success":
                http_status = attempt.get("http_status")
                receipt_sha = attempt.get("capture_receipt_sha256")
                if http_status not in (200, None):
                    raise ValueError(f"successful attempt for {target_id} must be HTTP 200 or a page reader capture")
                if http_status == 200 and receipt_sha is not None:
                    raise ValueError(f"HTTP success for {target_id} must not use a page reader receipt")
                if http_status is None and receipt_sha is None:
                    raise ValueError(f"successful attempt for {target_id} without HTTP status requires a capture receipt")
                evidence_sha = attempt.get("evidence_sha256")
                if not isinstance(evidence_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", evidence_sha):
                    raise ValueError(f"successful attempt for {target_id} has invalid evidence SHA")
                evidence_path = safe_import_path(import_dir, f"evidence/{evidence_sha}.json")
                raw = evidence_path.read_bytes()
                if sha256(raw) != evidence_sha:
                    raise ValueError(f"evidence SHA256 mismatch for {target_id}")
                normalized = normalize_config(raw, regions[target_id], plans, observed_at)
                if attempt.get("normalized") != normalized:
                    raise ValueError(f"normalized price evidence mismatch for {target_id}")
                if {item.get("plan_id") for item in normalized["plan_results"]} != {p["plan_id"] for p in plans}:
                    raise ValueError(f"normalized plan coverage mismatch for {target_id}")
                if http_status is None:
                    if not isinstance(receipt_sha, str):
                        raise ValueError(f"successful page reader attempt for {target_id} has invalid receipt SHA")
                    receipt_path = safe_import_path(import_dir, f"evidence/{receipt_sha}.json")
                    receipt_raw = receipt_path.read_bytes()
                    if sha256(receipt_raw) != receipt_sha:
                        raise ValueError(f"capture receipt SHA256 mismatch for {target_id}")
                    receipt = json.loads(receipt_raw.decode("utf-8"))
                    source_url = normalized["quotes"][0]["source"]["url"]
                    validate_page_reader_receipt(
                        raw, receipt, receipt_sha, evidence_sha, observed_at, source_url,
                    )
                    normalized = annotate_page_reader(normalized, receipt_sha)
                    evidence[receipt_sha] = receipt_path
                clean_attempts[target_id] = {
                    "kind": "success", "http_status": http_status, "normalized": normalized,
                    "observed_at": observed_at, "evidence_sha256": evidence_sha,
                    **({"capture_receipt_sha256": receipt_sha} if receipt_sha is not None else {}),
                }
                evidence[evidence_sha] = evidence_path
            elif kind in FAILURE_KINDS:
                if set(attempt) - FAILURE_FIELDS:
                    raise ValueError(f"failure attempt contains unsupported fields for {target_id}")
                http_status = attempt.get("http_status")
                reason = attempt.get("reason")
                if http_status is not None and (type(http_status) is not int or not 100 <= http_status <= 599):
                    raise ValueError(f"invalid failure HTTP status for {target_id}")
                if not isinstance(reason, str) or not reason:
                    raise ValueError(f"invalid failure reason for {target_id}")
                clean_attempts[target_id] = {
                    "kind": kind, "observed_at": observed_at, "reason": reason,
                    **({"http_status": http_status} if http_status is not None else {}),
                }
            else:
                raise ValueError(f"unsupported or unobserved attempt kind for {target_id}")
        counts = supplied.get("initial_fill_attempt_counts", {})
        if (not isinstance(counts, dict) or any(
                key not in attempts or type(value) is not int or value < 0
                for key, value in counts.items())):
            raise ValueError("invalid initial fill accounting")
        targets = [regions[key] for key in target_ids]
        rebuilt = {
            "created_unix": time.time(),
            "signature": checkpoint_signature(targets, plans),
            "attempts": clean_attempts,
            "initial_fill_attempt_counts": {key: counts[key] for key in target_ids if key in counts},
        }
        batches.append(ImportBatch(f"{spec['batch_id']}-{index + 1}", target_ids, rebuilt, evidence))
    return batches


def default_runner(command: list[str]):
    return subprocess.run(command, cwd=ROOT, text=True, capture_output=True,
                          encoding="utf-8", errors="replace")


def run_checked(runner, command: list[str]):
    result = runner(command)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command)}\n{result.stdout}\n{result.stderr}")
    return result


def execute_imports(batches, state_dir: Path, candidate_root: Path, public_dir: Path, runner=default_runner):
    python = sys.executable
    npm = "npm.cmd" if os.name == "nt" else "npm"
    pipeline = str(ROOT / "scripts/pipeline.py")
    candidate_root.mkdir(parents=True, exist_ok=True)
    for index, batch in enumerate(batches, 1):
        candidate = candidate_root / f"{index:02d}-{batch.batch_id}"
        if candidate.exists():
            shutil.rmtree(candidate)
        (candidate / "evidence").mkdir(parents=True)
        (candidate / "collection-checkpoint.json").write_text(
            json.dumps(batch.checkpoint, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        for evidence_sha, source in batch.evidence.items():
            shutil.copyfile(source, candidate / "evidence" / f"{evidence_sha}.json")
        prepare = [python, pipeline, "prepare", "--mode", "fill", "--targets", ",".join(batch.target_ids),
                   "--resume", "--budget-seconds", "0", "--state-dir", str(state_dir),
                   "--candidate-dir", str(candidate), "--public-dir", str(public_dir)]
        result = runner(prepare)
        if result.returncode:
            status_path = candidate / "state/status.json"
            ledger_path = candidate / "collection/ledger.json"
            run_path = candidate / "collection/last-run.json"
            status = read_json(status_path) if status_path.is_file() else {}
            ledger = read_json(ledger_path) if ledger_path.is_file() else None
            collection_run = read_json(run_path) if run_path.is_file() else None
            if status.get("failures") == [ALL_FAILED] and ledger and collection_run:
                run_checked(runner, [python, pipeline, "persist-collection", "--state-dir", str(state_dir),
                                     "--candidate-dir", str(candidate)])
                continue
            raise RuntimeError(f"import prepare failed ({result.returncode}): {result.stdout}\n{result.stderr}")
        run_checked(runner, [npm, "run", "build"])
        run_checked(runner, [python, pipeline, "verify-dist", "--dist-dir", "dist", "--candidate-dir", str(candidate)])
        run_checked(runner, [python, pipeline, "persist", "--state-dir", str(state_dir), "--candidate-dir", str(candidate)])

    final = candidate_root / "final-reuse"
    if final.exists():
        shutil.rmtree(final)
    run_checked(runner, [python, pipeline, "prepare", "--mode", "reuse", "--state-dir", str(state_dir),
                         "--candidate-dir", str(final), "--public-dir", str(public_dir)])
    run_checked(runner, [npm, "run", "build"])
    run_checked(runner, [python, pipeline, "verify-dist", "--dist-dir", "dist", "--candidate-dir", str(final)])
    run_checked(runner, [python, pipeline, "persist", "--state-dir", str(state_dir), "--candidate-dir", str(final)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--import-dir", default="import-state")
    parser.add_argument("--state-dir", default="data-state")
    parser.add_argument("--candidate-dir", default="work/import-candidate")
    parser.add_argument("--public-dir", default="public/data")
    args = parser.parse_args()
    try:
        batches = validate_imports(Path(args.import_dir))
        execute_imports(batches, Path(args.state_dir), Path(args.candidate_dir), Path(args.public_dir))
        print(json.dumps({"result": "imported", "batch_count": len(batches)}))
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"Observation import refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
