"""
Smoke test for the Regina Parcel Explorer API.

Prerequisites:
    The API is running and dbo.Parcel is loaded from the reference extract.

Usage:
    uv run python tests/api_smoke_test.py

Environment:
    API_BASE_URL  Base URL of the running API (default http://localhost:5107).

The KNOWN_* constants refer to one parcel in the reference extract. If the City
republishes the data with different object IDs, update them.
"""

import json
import math
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_URL = os.getenv("API_BASE_URL", "http://localhost:5107")

KNOWN_SOURCE_OBJECT_ID = 1509
KNOWN_LONGITUDE = -104.53753278221578
KNOWN_LATITUDE = 50.42169103325007

# A point in rural Saskatchewan, outside any parcel.
EMPTY_LONGITUDE = -103.5
EMPTY_LATITUDE = 50.0

# Far larger than any parcel, far smaller than an un-reprojected UTM coordinate.
POSITION_TOLERANCE_DEGREES = 0.1


class TestFailure(Exception):
    pass


def request(path: str, expected_status: int) -> object:
    url = f"{BASE_URL}{path}"

    try:
        with urlopen(
            Request(url, headers={"Accept": "application/json"}),
            timeout=10,
        ) as response:
            status = response.status
            body = response.read().decode("utf-8")

    except HTTPError as error:
        status = error.code
        body = error.read().decode("utf-8")

    except URLError as error:
        raise TestFailure(f"Could not reach API at {url}: {error}") from error

    if status != expected_status:
        raise TestFailure(
            f"{url} returned HTTP {status}; expected HTTP {expected_status}.\n"
            f"Response: {body}"
        )

    if not body:
        return None

    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise TestFailure(f"{url} returned invalid JSON.\nResponse: {body}") from error


def check_finite_coordinates(value: object) -> None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise TestFailure("GeoJSON contains a non-finite coordinate value.")
        return

    if isinstance(value, list):
        for item in value:
            check_finite_coordinates(item)
        return

    raise TestFailure("GeoJSON coordinates contain an unexpected value.")


def first_position(coordinates: object) -> list:
    while (
        isinstance(coordinates, list)
        and coordinates
        and isinstance(coordinates[0], list)
    ):
        coordinates = coordinates[0]

    if not isinstance(coordinates, list) or len(coordinates) < 2:
        raise TestFailure("GeoJSON coordinates do not contain a position.")

    return coordinates


def check_near_known_point(longitude: object, latitude: object, label: str) -> None:
    if not (isinstance(longitude, (int, float)) and isinstance(latitude, (int, float))):
        raise TestFailure(f"{label} is not a numeric longitude/latitude pair.")

    if (
        abs(longitude - KNOWN_LONGITUDE) > POSITION_TOLERANCE_DEGREES
        or abs(latitude - KNOWN_LATITUDE) > POSITION_TOLERANCE_DEGREES
    ):
        raise TestFailure(
            f"{label} ({longitude}, {latitude}) is not near the known point; "
            "the geometry may not be in WGS84."
        )


def test_get_known_parcel() -> None:
    parcel = request(f"/api/parcels/{KNOWN_SOURCE_OBJECT_ID}", 200)

    if not isinstance(parcel, dict):
        raise TestFailure("Known parcel response was not a JSON object.")

    if parcel.get("sourceObjectId") != KNOWN_SOURCE_OBJECT_ID:
        raise TestFailure(
            f"Expected sourceObjectId {KNOWN_SOURCE_OBJECT_ID}; "
            f"got {parcel.get('sourceObjectId')!r}."
        )

    if "parcelId" in parcel:
        raise TestFailure("The internal parcelId must not be exposed.")

    if not parcel.get("fullAddress"):
        raise TestFailure("Known parcel response did not contain an address.")

    check_near_known_point(
        parcel.get("centroidLongitude"),
        parcel.get("centroidLatitude"),
        "Parcel centroid",
    )


def test_find_parcels_at_known_point() -> None:
    parcels = request(
        f"/api/parcels/at-point?longitude={KNOWN_LONGITUDE}&latitude={KNOWN_LATITUDE}",
        200,
    )

    if not isinstance(parcels, list) or not parcels:
        raise TestFailure("Known spatial query did not return any parcels.")

    known_parcel = next(
        (
            parcel
            for parcel in parcels
            if parcel.get("sourceObjectId") == KNOWN_SOURCE_OBJECT_ID
        ),
        None,
    )

    if known_parcel is None:
        raise TestFailure(
            f"Known spatial query did not return source object {KNOWN_SOURCE_OBJECT_ID}."
        )

    if "parcelId" in known_parcel:
        raise TestFailure("The internal parcelId must not be exposed.")

    geometry = known_parcel.get("geometry")

    if not isinstance(geometry, dict):
        raise TestFailure("Spatial response did not contain a GeoJSON geometry object.")

    geometry_type = geometry.get("type")

    if geometry_type not in {"Polygon", "MultiPolygon"}:
        raise TestFailure(f"Unexpected GeoJSON geometry type: {geometry_type!r}.")

    coordinates = geometry.get("coordinates")

    if coordinates is None:
        raise TestFailure("GeoJSON geometry does not contain coordinates.")

    check_finite_coordinates(coordinates)

    longitude, latitude = first_position(coordinates)[:2]
    check_near_known_point(longitude, latitude, "First geometry position")


def test_no_parcel_at_point() -> None:
    parcels = request(
        f"/api/parcels/at-point?longitude={EMPTY_LONGITUDE}&latitude={EMPTY_LATITUDE}",
        200,
    )

    if parcels != []:
        raise TestFailure(f"Expected an empty list; got {parcels!r}.")


def test_invalid_parcel_id() -> None:
    request("/api/parcels/0", 400)


def test_unknown_parcel_id() -> None:
    request("/api/parcels/999999999", 404)


def test_missing_spatial_coordinates() -> None:
    request("/api/parcels/at-point", 400)


def test_invalid_spatial_coordinates() -> None:
    request("/api/parcels/at-point?longitude=181&latitude=50", 400)


def main() -> int:
    tests = [
        ("known parcel lookup", test_get_known_parcel),
        ("known spatial lookup", test_find_parcels_at_known_point),
        ("no parcel at point", test_no_parcel_at_point),
        ("invalid parcel ID", test_invalid_parcel_id),
        ("unknown parcel ID", test_unknown_parcel_id),
        ("missing spatial coordinates", test_missing_spatial_coordinates),
        ("invalid spatial coordinates", test_invalid_spatial_coordinates),
    ]

    print(f"Testing API: {BASE_URL}")
    print()

    failures = 0

    for name, test in tests:
        try:
            test()
            print(f"PASS  {name}")
        except TestFailure as error:
            failures += 1
            print(f"FAIL  {name}")
            print(f"      {error}")

    print()

    if failures:
        print(f"{failures} test(s) failed.")
        return 1

    print(f"All {len(tests)} API smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
