"""
Validate the raw Regina TaxWeb parcel extraction.

Source:
    data/raw/regina_taxweb_parcels.json (written by etl/extract_parcels.py)
    Required keys: features, record_count, source_record_count,
    spatial_reference. The keys source_url and retrieved_at_utc are copied into
    the report when present.

Destination:
    artifacts/parcel-validation-report.json (generated, not committed)

Usage:
    uv run python etl/validate_parcels.py

Status:
    FAIL                Blocking problems found; exits with a non-zero status.
    PASS_WITH_WARNINGS  Loadable data is sound, but some records are excluded
                        or incomplete (see "warnings" in the report).
    PASS                No problems found.

"failures" and "warnings" in the report list the checks that triggered.

Geometry checks are structural only: closed rings, at least four points and
finite two-dimensional coordinates. Topological validity (self-intersections,
hole assignment) is enforced by load_parcels.py and confirmed in SQL Server
with STIsValid(). A record is "loadable" under the same rule the loader uses:
TAS_ACCT_ID > 0.
"""

import json
import math
import tempfile
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]

INPUT_PATH = PROJECT_ROOT / "data" / "raw" / "regina_taxweb_parcels.json"

OUTPUT_PATH = PROJECT_ROOT / "artifacts" / "parcel-validation-report.json"

EXPECTED_SRID = 26913

GEOMETRY_OK = "structurally_valid"


def load_extract(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Raw extract not found: {path}")

    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Raw extract contains invalid JSON: {path}") from exc

    if not isinstance(data, dict):
        raise ValueError("Raw extract must contain a JSON object.")

    if not isinstance(data.get("features"), list):
        raise ValueError("Raw extract does not contain a feature list.")

    return data


def parse_assessed_value(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None

    text = str(value).replace(",", "").strip()

    if not text:
        return None

    try:
        decimal_value = Decimal(text)
    except InvalidOperation:
        return None

    if not decimal_value.is_finite():
        return None

    return decimal_value


def is_finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def validate_ring(ring: object) -> bool:
    if not isinstance(ring, list) or len(ring) < 4 or ring[0] != ring[-1]:
        return False

    return all(
        isinstance(point, list)
        and len(point) == 2
        and all(is_finite_number(coordinate) for coordinate in point)
        for point in ring
    )


def validate_geometry(geometry: object) -> str:
    if not isinstance(geometry, dict):
        return "missing_or_invalid_geometry"

    rings = geometry.get("rings")

    if not isinstance(rings, list):
        return "missing_rings"

    if not rings:
        return "empty_rings"

    if not all(validate_ring(ring) for ring in rings):
        return "invalid_ring_structure"

    return GEOMETRY_OK


def classify_object_id(object_id: object) -> str:
    if object_id is None:
        return "missing"

    if isinstance(object_id, int) and not isinstance(object_id, bool) and object_id > 0:
        return "valid"

    return "invalid"


def classify_account(account: object) -> str:
    if account is None:
        return "missing"

    if not isinstance(account, int) or isinstance(account, bool):
        return "invalid"

    if account == 0:
        return "zero"

    if account < 0:
        return "invalid"

    return "loadable"


def has_address(address: object) -> bool:
    return isinstance(address, str) and bool(address.strip())


def classify_assessment(value: object) -> str:
    if value is None:
        return "missing"

    parsed_value = parse_assessed_value(value)

    if parsed_value is None:
        return "invalid"

    if parsed_value < 0:
        return "negative"

    return "valid"


def spatial_reference_matches(spatial_reference: object) -> bool:
    return isinstance(spatial_reference, dict) and EXPECTED_SRID in {
        spatial_reference.get("wkid"),
        spatial_reference.get("latestWkid"),
    }


def build_report(data: dict[str, Any]) -> dict[str, Any]:
    features = data["features"]

    counts: Counter[str] = Counter()
    geometry_status_counts: Counter[str] = Counter()
    source_object_id_counts: Counter[int] = Counter()
    account_counts: Counter[int] = Counter()

    for feature in features:
        if not isinstance(feature, dict):
            counts["invalid_feature"] += 1
            geometry_status_counts["invalid_feature"] += 1
            continue

        attributes = feature.get("attributes")

        if not isinstance(attributes, dict):
            counts["invalid_attributes"] += 1
            attributes = {}

        object_id = attributes.get("OBJECTID")
        object_id_status = classify_object_id(object_id)

        if object_id_status == "valid":
            source_object_id_counts[object_id] += 1
        else:
            counts[f"{object_id_status}_source_object_id"] += 1

        geometry = feature.get("geometry")
        geometry_status = validate_geometry(geometry)
        geometry_status_counts[geometry_status] += 1

        if geometry_status == GEOMETRY_OK and len(geometry["rings"]) > 1:
            counts["multi_ring_features"] += 1

        account = attributes.get("TAS_ACCT_ID")
        account_status = classify_account(account)
        loadable = account_status == "loadable"

        if loadable:
            account_counts[account] += 1
        else:
            counts[f"{account_status}_account"] += 1

        if not has_address(attributes.get("FULL_ADDRESS")):
            counts["missing_address"] += 1

            if loadable:
                counts["missing_loadable_address"] += 1

        assessment_status = classify_assessment(attributes.get("ASMT_CHAR"))

        if assessment_status != "valid":
            counts[f"{assessment_status}_assessment"] += 1

            if loadable:
                counts[f"{assessment_status}_loadable_assessment"] += 1

    duplicate_source_object_ids = sorted(
        object_id for object_id, count in source_object_id_counts.items() if count > 1
    )

    duplicate_account_ids = sorted(
        account for account, count in account_counts.items() if count > 1
    )

    duplicate_account_excess_records = sum(
        count - 1 for count in account_counts.values() if count > 1
    )

    geometry_failures = sum(
        count
        for status, count in geometry_status_counts.items()
        if status != GEOMETRY_OK
    )

    record_count_match = (
        isinstance(data.get("record_count"), int)
        and isinstance(data.get("source_record_count"), int)
        and data["record_count"] == len(features)
        and data["source_record_count"] == len(features)
    )

    spatial_reference = data.get("spatial_reference")
    spatial_reference_match = spatial_reference_matches(spatial_reference)

    failure_checks = {
        "invalid_features": counts["invalid_feature"] > 0,
        "invalid_attributes": counts["invalid_attributes"] > 0,
        "missing_source_object_id": counts["missing_source_object_id"] > 0,
        "invalid_source_object_id": counts["invalid_source_object_id"] > 0,
        "duplicate_source_object_ids": bool(duplicate_source_object_ids),
        "record_count_mismatch": not record_count_match,
        "spatial_reference_mismatch": not spatial_reference_match,
        "invalid_geometry": geometry_failures > 0,
        "missing_account": counts["missing_account"] > 0,
        "invalid_account": counts["invalid_account"] > 0,
        "missing_loadable_address": counts["missing_loadable_address"] > 0,
        "missing_loadable_assessment": counts["missing_loadable_assessment"] > 0,
        "invalid_loadable_assessment": counts["invalid_loadable_assessment"] > 0,
        "negative_loadable_assessment": counts["negative_loadable_assessment"] > 0,
    }

    warning_checks = {
        "zero_account": counts["zero_account"] > 0,
        "missing_address": counts["missing_address"] > 0,
        "missing_assessment": counts["missing_assessment"] > 0,
        "invalid_assessment": counts["invalid_assessment"] > 0,
        "negative_assessment": counts["negative_assessment"] > 0,
        "duplicate_account_ids": bool(duplicate_account_ids),
    }

    failures = [name for name, triggered in failure_checks.items() if triggered]
    warnings = [name for name, triggered in warning_checks.items() if triggered]

    if failures:
        status = "FAIL"
    elif warnings:
        status = "PASS_WITH_WARNINGS"
    else:
        status = "PASS"

    return {
        "validated_at_utc": datetime.now(UTC).isoformat(),
        "source": {
            "url": data.get("source_url"),
            "retrieved_at_utc": data.get("retrieved_at_utc"),
            "reported_record_count": data.get("source_record_count"),
            "extracted_record_count": data.get("record_count"),
            "actual_feature_count": len(features),
            "record_count_match": record_count_match,
            "spatial_reference": spatial_reference,
            "spatial_reference_match": spatial_reference_match,
        },
        "geometry": {
            "status_counts": dict(geometry_status_counts),
            "multi_ring_features": counts["multi_ring_features"],
        },
        "attributes": {
            "invalid_feature": counts["invalid_feature"],
            "invalid_attributes": counts["invalid_attributes"],
            "missing_source_object_id": counts["missing_source_object_id"],
            "invalid_source_object_id": counts["invalid_source_object_id"],
            "duplicate_source_object_id_count": len(duplicate_source_object_ids),
            "duplicate_source_object_ids": duplicate_source_object_ids,
            "missing_account": counts["missing_account"],
            "invalid_account": counts["invalid_account"],
            "zero_account": counts["zero_account"],
            "missing_address": counts["missing_address"],
            "missing_loadable_address": counts["missing_loadable_address"],
            "missing_assessment": counts["missing_assessment"],
            "invalid_assessment": counts["invalid_assessment"],
            "negative_assessment": counts["negative_assessment"],
            "missing_loadable_assessment": counts["missing_loadable_assessment"],
            "invalid_loadable_assessment": counts["invalid_loadable_assessment"],
            "negative_loadable_assessment": counts["negative_loadable_assessment"],
            "duplicate_account_id_count": len(duplicate_account_ids),
            "duplicate_account_ids": duplicate_account_ids,
            "duplicate_account_excess_records": duplicate_account_excess_records,
        },
        "failures": failures,
        "warnings": warnings,
        "status": status,
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary_path = Path(file.name)

            json.dump(report, file, indent=2)
            file.write("\n")

        temporary_path.replace(path)

    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main() -> None:
    print("Validating Regina TaxWeb parcel extraction")

    data = load_extract(INPUT_PATH)

    report = build_report(data)

    print()
    print("=" * 64)
    print("REGINA PARCEL VALIDATION")
    print("=" * 64)
    print(f"Features: {report['source']['actual_feature_count']}")
    print(f"Status: {report['status']}")

    print()
    print("Geometry:")
    print(json.dumps(report["geometry"], indent=2))

    print()
    print("Attributes:")
    print(json.dumps(report["attributes"], indent=2))

    print()
    print(f"Failures: {report['failures'] or 'none'}")
    print(f"Warnings: {report['warnings'] or 'none'}")

    write_report(OUTPUT_PATH, report)

    print()
    print(f"Written: {OUTPUT_PATH}")

    if report["status"] == "FAIL":
        raise SystemExit(
            "Parcel validation failed: "
            + ", ".join(report["failures"])
            + f". See {OUTPUT_PATH}"
        )


if __name__ == "__main__":
    main()
