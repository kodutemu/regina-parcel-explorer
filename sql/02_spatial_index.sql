/*
    Spatial index on dbo.Parcel.ParcelGeometry.

    BOUNDING_BOX is in EPSG:26913 metres and covers Regina's parcels with a
    margin. Geometry outside the box is not usefully indexed, so widen the box
    if the data grows beyond it. To confirm the box covers every loaded parcel
    (expect 0):

        DECLARE @Box geometry = geometry::STGeomFromText(
            'POLYGON((515500 5582500, 536500 5582500, 536500 5597000,
                      515500 5597000, 515500 5582500))', 26913);
        SELECT COUNT(*) AS ParcelsOutsideBox
        FROM dbo.Parcel
        WHERE ParcelGeometry.STWithin(@Box) = 0;

    A bounding box cannot be altered, so this script drops and re-creates the
    index. The ODBC sqlcmd defaults QUOTED_IDENTIFIER to OFF unless -I is used,
    so the session options are set explicitly.
*/

SET ANSI_NULLS ON;
SET ANSI_PADDING ON;
SET ANSI_WARNINGS ON;
SET ARITHABORT ON;
SET CONCAT_NULL_YIELDS_NULL ON;
SET QUOTED_IDENTIFIER ON;
SET NUMERIC_ROUNDABORT OFF;
GO

USE ReginaParcelExplorer;
GO

IF EXISTS
(
    SELECT 1
    FROM sys.spatial_indexes
    WHERE object_id = OBJECT_ID(N'dbo.Parcel')
      AND name = N'IX_Parcel_ParcelGeometry'
)
BEGIN
    DROP INDEX IX_Parcel_ParcelGeometry ON dbo.Parcel;
END;
GO

CREATE SPATIAL INDEX IX_Parcel_ParcelGeometry
ON dbo.Parcel (ParcelGeometry)
USING GEOMETRY_AUTO_GRID
WITH
(
    BOUNDING_BOX =
    (
        XMIN = 515500,
        YMIN = 5582500,
        XMAX = 536500,
        YMAX = 5597000
    )
);
GO
