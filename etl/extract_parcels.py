"""
Extract Regina TaxWeb parcels from the City of Regina ArcGIS REST service.

Source:
    https://opengis.regina.ca/arcgis/rest/services/CGISViewer/Taxweb/MapServer/0
    Published under the City of Regina Open Government Licence.

Destination:
    data/raw/regina_taxweb_parcels.json

Usage:
    uv run python etl/extract_parcels.py

Output contract (read by the validate and load scripts):
    source_url            Layer URL the data was requested from.
    retrieved_at_utc      UTC timestamp of the completed download.
    record_count          Number of features downloaded.
    source_record_count   Record count reported by the service.
    spatial_reference     Spatial reference reported by the service (EPSG:26913).
    features              ArcGIS JSON features with the attributes in OUT_FIELDS
                          and polygon rings in the spatial reference above.
"""

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


PROJECT_ROOT = Path(__file__).resolve().parents[1]

SOURCE_URL = (
    "https://opengis.regina.ca/arcgis/rest/services/CGISViewer/Taxweb/MapServer/0"
)

OUTPUT_PATH = PROJECT_ROOT / "data" / "raw" / "regina_taxweb_parcels.json"

OUTPUT_SRID = 26913
OUT_FIELDS = "OBJECTID,TAS_ACCT_ID,FULL_ADDRESS,ASMT_CHAR,MULTI_ACCTS"

PAGE_SIZE = 10_000
TIMEOUT_SECONDS = 60

USER_AGENT = "ReginaParcelExplorer/0.1"


def create_session() -> requests.Session:
    # raise_on_status=False returns the last response once retries are used up,
    # so raise_for_status() reports the real HTTP status.
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.mount("https://", HTTPAdapter(max_retries=retry))

    return session


def fetch_json(
    session: requests.Session,
    url: str,
    params: dict[str, str],
) -> dict[str, Any]:
    try:
        response = session.get(url, params=params, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()

    except requests.Timeout as exc:
        raise RuntimeError(
            f"ArcGIS request timed out after {TIMEOUT_SECONDS} seconds."
        ) from exc

    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        reason = exc.response.reason if exc.response is not None else str(exc)

        raise RuntimeError(
            f"ArcGIS request failed with HTTP {status}: {reason}"
        ) from exc

    except requests.JSONDecodeError as exc:
        raise RuntimeError("ArcGIS service returned invalid JSON.") from exc

    except requests.RequestException as exc:
        raise RuntimeError(f"Unable to reach ArcGIS service: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError("ArcGIS service returned an unexpected JSON response.")

    if "error" in data:
        raise RuntimeError(f"ArcGIS service returned an error: {data['error']}")

    return data


def fetch_total_count(session: requests.Session) -> int:
    result = fetch_json(
        session,
        f"{SOURCE_URL}/query",
        {
            "where": "1=1",
            "returnCountOnly": "true",
            "f": "json",
        },
    )

    try:
        return int(result["count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "ArcGIS count response did not contain a valid record count."
        ) from exc


def fetch_page(
    session: requests.Session,
    offset: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    result = fetch_json(
        session,
        f"{SOURCE_URL}/query",
        {
            "where": "1=1",
            "outFields": OUT_FIELDS,
            "returnGeometry": "true",
            "outSR": str(OUTPUT_SRID),
            "orderByFields": "OBJECTID ASC",
            "resultOffset": str(offset),
            "resultRecordCount": str(PAGE_SIZE),
            "f": "json",
        },
    )

    features = result.get("features")

    if not isinstance(features, list):
        raise RuntimeError("ArcGIS query response did not contain a feature list.")

    spatial_reference = result.get("spatialReference")

    if not isinstance(spatial_reference, dict):
        raise RuntimeError("ArcGIS query response did not report a spatial reference.")

    reported = {spatial_reference.get("wkid"), spatial_reference.get("latestWkid")}

    if OUTPUT_SRID not in reported:
        raise RuntimeError(
            f"ArcGIS returned spatial reference {spatial_reference}; "
            f"expected EPSG:{OUTPUT_SRID}."
        )

    return features, spatial_reference


def write_extract(
    path: Path,
    features: list[dict[str, Any]],
    total_count: int,
    spatial_reference: dict[str, Any],
) -> None:
    payload = {
        "source_url": SOURCE_URL,
        "retrieved_at_utc": datetime.now(UTC).isoformat(),
        "record_count": len(features),
        "source_record_count": total_count,
        "spatial_reference": spatial_reference,
        "features": features,
    }

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

            json.dump(payload, file, ensure_ascii=False)
            file.write("\n")

        temporary_path.replace(path)

    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main() -> None:
    print("Starting Regina TaxWeb parcel extraction")

    features: list[dict[str, Any]] = []
    spatial_reference: dict[str, Any] = {}

    with create_session() as session:
        total_count = fetch_total_count(session)

        print(f"Total records reported by source: {total_count}")

        if total_count <= 0:
            raise RuntimeError("ArcGIS service reported no records.")

        while len(features) < total_count:
            page, spatial_reference = fetch_page(session, len(features))

            if not page:
                raise RuntimeError(
                    "ArcGIS returned an empty page before the expected record "
                    f"count was reached. Retrieved {len(features)} of "
                    f"{total_count} records."
                )

            features.extend(page)

            print(f"Retrieved {len(features)} / {total_count}")

    if len(features) != total_count:
        raise RuntimeError(
            f"Extraction count mismatch: retrieved {len(features)}, "
            f"source reported {total_count}."
        )

    write_extract(OUTPUT_PATH, features, total_count, spatial_reference)

    print(f"Extraction complete: {len(features)} records")
    print(f"Written: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
