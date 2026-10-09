/*
    Example spatial queries for dbo.Parcel (not part of setup).

    Run with sqlcmd or SSMS. sqlcmd reads the password from the SQLCMDPASSWORD
    environment variable, or prompts for it:
        sqlcmd -S localhost,1433 -U <login> -C -i sql/examples/spatial_queries.sql

    Geometry is stored in EPSG:26913, so areas are in square metres and
    distances are in metres.
*/

USE ReginaParcelExplorer;

-- Set a SourceObjectId, or leave NULL to use the lowest one.
DECLARE @ReferenceSourceObjectId int = NULL;

IF @ReferenceSourceObjectId IS NULL
BEGIN
    SELECT @ReferenceSourceObjectId = MIN(SourceObjectId)
    FROM dbo.Parcel;
END;

DECLARE @ReferenceGeometry geometry;
DECLARE @ReferencePoint geometry;

SELECT @ReferenceGeometry = ParcelGeometry
FROM dbo.Parcel
WHERE SourceObjectId = @ReferenceSourceObjectId;

IF @ReferenceGeometry IS NULL
BEGIN
    THROW 50001, 'Reference parcel was not found.', 1;
END;

-- A point inside the parcel (a centroid can fall outside an irregular parcel).
SET @ReferencePoint = @ReferenceGeometry.STPointOnSurface();

------------------------------------------------------------
-- 1. Parcel measurements
------------------------------------------------------------

SELECT
    SourceObjectId,
    TasAcctId,
    FullAddress,
    ParcelGeometry.STGeometryType() AS GeometryType,
    ParcelGeometry.STSrid AS Srid,
    ParcelGeometry.STArea() AS AreaSquareMeters,
    @ReferencePoint.STX AS ReferencePointX,
    @ReferencePoint.STY AS ReferencePointY
FROM dbo.Parcel
WHERE SourceObjectId = @ReferenceSourceObjectId;

------------------------------------------------------------
-- 2. Neighbouring parcels (adjacent parcels share a boundary, so
--    STIntersects returns them)
------------------------------------------------------------

SELECT
    p.SourceObjectId,
    p.TasAcctId,
    p.FullAddress,
    p.ParcelGeometry.STTouches(@ReferenceGeometry) AS TouchesReference,
    p.ParcelGeometry.STOverlaps(@ReferenceGeometry) AS OverlapsReference
FROM dbo.Parcel AS p
WHERE p.SourceObjectId <> @ReferenceSourceObjectId
  AND p.ParcelGeometry.STIntersects(@ReferenceGeometry) = 1
ORDER BY p.SourceObjectId;

------------------------------------------------------------
-- 3. Ten nearest parcels to the reference point. Written to meet the
--    documented requirements for a spatial-index nearest-neighbour query:
--    TOP, STDistance on the indexed column in WHERE (IS NOT NULL, joined by
--    AND) and as the first ascending ORDER BY term. Both geometries must have
--    the same SRID, otherwise STDistance returns NULL.
------------------------------------------------------------

SELECT TOP (10)
    p.SourceObjectId,
    p.TasAcctId,
    p.FullAddress,
    p.ParcelGeometry.STDistance(@ReferencePoint) AS DistanceMeters
FROM dbo.Parcel AS p
WHERE p.SourceObjectId <> @ReferenceSourceObjectId
  AND p.ParcelGeometry.STDistance(@ReferencePoint) IS NOT NULL
ORDER BY
    p.ParcelGeometry.STDistance(@ReferencePoint),
    p.SourceObjectId;

------------------------------------------------------------
-- 4. Parcels containing the reference point (the same predicate the API
--    uses for a map click)
------------------------------------------------------------

SELECT
    SourceObjectId,
    TasAcctId,
    FullAddress
FROM dbo.Parcel
WHERE ParcelGeometry.STIntersects(@ReferencePoint) = 1
ORDER BY SourceObjectId;
