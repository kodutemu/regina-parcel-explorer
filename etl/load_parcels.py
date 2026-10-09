"""
Load the Regina TaxWeb parcel extraction into SQL Server.

Source:
    data/raw/regina_taxweb_parcels.json (written by etl/extract_parcels.py)
    Required keys: record_count, source_record_count, spatial_reference, features.
    Per feature: attributes OBJECTID, TAS_ACCT_ID, FULL_ADDRESS, ASMT_CHAR,
    MULTI_ACCTS, and geometry rings in EPSG:26913.

Destination:
    ReginaParcelExplorer.dbo.Parcel   (full refresh)
    ReginaParcelExplorer.dbo.EtlRun   (run tracking)

Usage:
    uv run python etl/load_parcels.py

Rules:
    Records with TAS_ACCT_ID <= 0 are excluded.
    Geometry is stored as geometry in EPSG:26913.
    The refresh, the row-count and geometry checks, and the EtlRun success
    record are committed in a single transaction. TRUNCATE takes a schema lock
    on dbo.Parcel, so API reads wait briefly until the transaction commits.
    A failed or interrupted run is rolled back and recorded as Failed.

Configuration (environment or .env, see .env.example):
    SQL_SERVER, SQL_DATABASE, SQL_USER, SQL_PASSWORD
    SQL_TRUST_SERVER_CERTIFICATE ("yes" or "no", default "no")
"""

import json
import os
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

import mssql_python
from dotenv import load_dotenv
from shapely.geometry import LinearRing, MultiPolygon, Polygon


PROJECT_ROOT = Path(__file__).resolve().parents[1]

SOURCE_PATH = PROJECT_ROOT / "data" / "raw" / "regina_taxweb_parcels.json"

SOURCE_SRID = 26913

BATCH_SIZE = 5000
ERROR_MESSAGE_LIMIT = 4000

STAGING_COLUMNS = [
    "SourceObjectId",
    "TasAcctId",
    "FullAddress",
    "AssessedValue",
    "MultiAccts",
    "ParcelWkb",
]


def require_env(name: str) -> str:
    value = os.environ.get(name)

    if not value:
        raise RuntimeError(f"Missing environment variable {name}. See .env.example.")

    return value


def get_connection() -> Any:
    trust_certificate = os.environ.get("SQL_TRUST_SERVER_CERTIFICATE", "no").lower()

    if trust_certificate not in {"yes", "no"}:
        raise RuntimeError('SQL_TRUST_SERVER_CERTIFICATE must be "yes" or "no".')

    return mssql_python.connect(
        server=require_env("SQL_SERVER"),
        database=require_env("SQL_DATABASE"),
        uid=require_env("SQL_USER"),
        pwd=require_env("SQL_PASSWORD"),
        encrypt="yes",
        trust_server_certificate=trust_certificate,
        autocommit=False,
        timeout=30,
    )


def get_staging_table_name() -> str:
    # Global temp table: bulkcopy() uses its own connection, so a session-scoped
    # #temp table would not be visible to it. The UUID prevents name collisions.
    return f"##ReginaParcelLoad_{uuid4().hex}"


def arcgis_rings_to_wkb(rings: list[list[list[float]]], object_id: int) -> bytes:
    """Convert ArcGIS polygon rings to Shapely WKB.

    ArcGIS exterior rings are clockwise and holes are counter-clockwise.
    """
    if not rings:
        raise RuntimeError(f"OBJECTID {object_id} has no geometry rings.")

    exteriors: list[Polygon] = []
    holes: list[Polygon] = []

    for ring in rings:
        if len(ring) < 4:
            raise RuntimeError(
                f"OBJECTID {object_id} contains a ring with too few points."
            )

        linear_ring = LinearRing([(point[0], point[1]) for point in ring])

        if not linear_ring.is_closed:
            raise RuntimeError(f"OBJECTID {object_id} contains an open ring.")

        polygon = Polygon(linear_ring)

        if polygon.is_empty or not polygon.is_valid:
            raise RuntimeError(f"OBJECTID {object_id} contains an invalid ring.")

        if linear_ring.is_ccw:
            holes.append(polygon)
        else:
            exteriors.append(polygon)

    if not exteriors:
        raise RuntimeError(f"OBJECTID {object_id} has no exterior ring.")

    exterior_holes: list[list[Any]] = [[] for _ in exteriors]

    for hole in holes:
        point = hole.representative_point()

        containing = [
            index for index, exterior in enumerate(exteriors) if exterior.covers(point)
        ]

        if not containing:
            raise RuntimeError(f"OBJECTID {object_id} contains an unassigned hole.")

        exterior_index = min(containing, key=lambda index: exteriors[index].area)

        exterior_holes[exterior_index].append(list(hole.exterior.coords))

    polygons = [
        Polygon(exterior.exterior.coords, holes_for_exterior)
        for exterior, holes_for_exterior in zip(exteriors, exterior_holes)
    ]

    geometry = polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)

    if geometry.is_empty or not geometry.is_valid:
        raise RuntimeError(f"OBJECTID {object_id} produced invalid geometry.")

    return geometry.wkb


def parse_assessed_value(value: object, object_id: int) -> Decimal:
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation as exc:
        raise RuntimeError(
            f"OBJECTID {object_id} has a non-numeric assessment: {value!r}"
        ) from exc

    if not amount.is_finite():
        raise RuntimeError(
            f"OBJECTID {object_id} has a non-finite assessment: {value!r}"
        )

    return amount


def load_source() -> dict[str, Any]:
    if not SOURCE_PATH.exists():
        raise FileNotFoundError(f"Source file not found: {SOURCE_PATH}")

    try:
        with SOURCE_PATH.open("r", encoding="utf-8") as file:
            source = json.load(file)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Source file contains invalid JSON: {SOURCE_PATH}") from exc

    if not isinstance(source, dict):
        raise RuntimeError("Source file must contain a JSON object.")

    features = source.get("features")

    if not isinstance(features, list):
        raise RuntimeError("Source file does not contain a feature list.")

    for key in ("record_count", "source_record_count"):
        value = source.get(key)

        if not isinstance(value, int) or value != len(features):
            raise RuntimeError(f"Source {key} does not match the actual feature count.")

    spatial_reference = source.get("spatial_reference")

    if not isinstance(spatial_reference, dict) or SOURCE_SRID not in {
        spatial_reference.get("wkid"),
        spatial_reference.get("latestWkid"),
    }:
        raise RuntimeError(
            f"Source spatial reference is {spatial_reference!r}; "
            f"expected EPSG:{SOURCE_SRID}."
        )

    return source


def rows_to_load(features: list[dict[str, Any]]) -> Iterator[tuple[Any, ...]]:
    for feature in features:
        attributes = feature["attributes"]

        object_id = int(attributes["OBJECTID"])
        tas_acct_id = int(attributes["TAS_ACCT_ID"])

        if tas_acct_id <= 0:
            continue

        address = attributes["FULL_ADDRESS"]

        if not isinstance(address, str) or not address.strip():
            raise RuntimeError(f"OBJECTID {object_id} has no usable address.")

        yield (
            object_id,
            tas_acct_id,
            address,
            parse_assessed_value(attributes["ASMT_CHAR"], object_id),
            attributes.get("MULTI_ACCTS"),
            arcgis_rings_to_wkb(feature["geometry"]["rings"], object_id),
        )


def create_staging_table(cursor: Any, staging_table: str) -> None:
    # SQL identifiers cannot be parameterized. The table name is generated by
    # get_staging_table_name() and is never derived from external input.
    cursor.execute(
        f"""
        CREATE TABLE {staging_table}
        (
            SourceObjectId  int             NOT NULL,
            TasAcctId       int             NOT NULL,
            FullAddress     nvarchar(255)   NOT NULL,
            AssessedValue   decimal(18,2)   NOT NULL,
            MultiAccts      nvarchar(50)    NULL,
            ParcelWkb       varbinary(max)  NOT NULL
        );
        """
    )


def drop_staging_table(cursor: Any, staging_table: str) -> None:
    cursor.execute(f"DROP TABLE IF EXISTS {staging_table};")


def stage_rows(cursor: Any, staging_table: str, features: list[dict[str, Any]]) -> int:
    result = cursor.bulkcopy(
        staging_table,
        rows_to_load(features),
        column_mappings=STAGING_COLUMNS,
        batch_size=BATCH_SIZE,
        table_lock=True,
        keep_nulls=True,
    )

    return result["rows_copied"]


def replace_parcel_table(cursor: Any, staging_table: str) -> None:
    # LoadedAtUtc is filled by the column default in sql/01_schema.sql.
    cursor.execute("TRUNCATE TABLE dbo.Parcel;")

    cursor.execute(
        f"""
        INSERT INTO dbo.Parcel
        (
            SourceObjectId,
            TasAcctId,
            FullAddress,
            AssessedValue,
            MultiAccts,
            ParcelGeometry
        )
        SELECT
            SourceObjectId,
            TasAcctId,
            FullAddress,
            AssessedValue,
            MultiAccts,
            geometry::STGeomFromWKB(ParcelWkb, {SOURCE_SRID})
        FROM {staging_table};
        """
    )


def create_etl_run(connection: Any) -> int:
    cursor = connection.cursor()

    cursor.execute(
        """
        INSERT INTO dbo.EtlRun (StartedAtUtc, RunStatus)
        OUTPUT INSERTED.EtlRunId
        VALUES (SYSUTCDATETIME(), N'Started');
        """
    )

    run_id = cursor.fetchone()[0]

    connection.commit()

    return run_id


def update_run_progress(
    connection: Any,
    run_id: int,
    source_count: int,
    excluded_count: int,
) -> None:
    cursor = connection.cursor()

    cursor.execute(
        """
        UPDATE dbo.EtlRun
        SET SourceRecordCount = ?, ExcludedRecordCount = ?
        WHERE EtlRunId = ?;
        """,
        (source_count, excluded_count, run_id),
    )

    connection.commit()


def record_run_success(cursor: Any, run_id: int, loaded_count: int) -> None:
    cursor.execute(
        """
        UPDATE dbo.EtlRun
        SET CompletedAtUtc = SYSUTCDATETIME(),
            LoadedRecordCount = ?,
            RunStatus = N'Succeeded'
        WHERE EtlRunId = ?;
        """,
        (loaded_count, run_id),
    )


def mark_run_failed(run_id: int, error_message: str) -> None:
    # Uses its own connection so the failure record survives the rollback.
    connection = None

    try:
        connection = get_connection()
        cursor = connection.cursor()

        cursor.execute(
            """
            UPDATE dbo.EtlRun
            SET CompletedAtUtc = SYSUTCDATETIME(),
                RunStatus = N'Failed',
                ErrorMessage = ?
            WHERE EtlRunId = ?;
            """,
            (error_message[:ERROR_MESSAGE_LIMIT], run_id),
        )

        connection.commit()

    except Exception as tracking_error:
        print(f"WARNING: unable to record ETL failure: {tracking_error}")

    finally:
        if connection is not None:
            connection.close()


def verify_destination(cursor: Any, expected_count: int) -> int:
    cursor.execute(
        """
        SELECT
            COUNT(*),
            SUM(CASE WHEN ParcelGeometry.STIsValid() = 0 THEN 1 ELSE 0 END)
        FROM dbo.Parcel;
        """
    )

    destination_count, invalid_count = cursor.fetchone()

    if destination_count != expected_count:
        raise RuntimeError(
            f"Destination contains {destination_count} rows; expected {expected_count}."
        )

    if invalid_count:
        raise RuntimeError(
            f"Destination contains {invalid_count} geometries that SQL Server "
            "reports as invalid."
        )

    return destination_count


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")

    print("Loading Regina TaxWeb parcels")

    connection = None
    cursor = None
    run_id = None
    staging_table = get_staging_table_name()

    try:
        connection = get_connection()
        cursor = connection.cursor()

        run_id = create_etl_run(connection)

        print(f"ETL run ID: {run_id}")

        source = load_source()
        features = source["features"]
        source_count = len(features)

        print(f"Source features: {source_count}")

        create_staging_table(cursor, staging_table)
        connection.commit()

        loaded_count = stage_rows(cursor, staging_table, features)
        excluded_count = source_count - loaded_count

        print(f"Rows prepared for load: {loaded_count}")
        print(f"Excluded by load rule: {excluded_count}")

        update_run_progress(connection, run_id, source_count, excluded_count)

        replace_parcel_table(cursor, staging_table)

        destination_count = verify_destination(cursor, loaded_count)

        record_run_success(cursor, run_id, destination_count)

        connection.commit()

        print(f"Rows in SQL Server: {destination_count}")

    except BaseException as exception:
        # BaseException so an interrupted run (Ctrl+C) is also recorded.
        if connection is not None:
            try:
                connection.rollback()
            except Exception as rollback_error:
                print(f"WARNING: rollback failed: {rollback_error}")

        if run_id is not None:
            mark_run_failed(run_id, f"{type(exception).__name__}: {exception}")

        raise

    finally:
        if connection is not None:
            if cursor is not None:
                try:
                    drop_staging_table(cursor, staging_table)
                    connection.commit()
                except Exception as cleanup_error:
                    print(f"WARNING: unable to drop staging table: {cleanup_error}")

            connection.close()

    print("SQL Server parcel load: PASS")


if __name__ == "__main__":
    main()
