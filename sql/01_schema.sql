/*
    Regina Parcel Explorer - database schema

    Source:
        City of Regina TaxWeb parcel layer (Open Government Licence)

    Parcel coordinate system:
        EPSG:26913 (NAD83 / UTM zone 13N, units are metres)

    Running this script:
        GO is a batch separator understood by sqlcmd and SSMS, not by database
        drivers. For example:
            sqlcmd -S localhost,1433 -U <admin login> -C -i sql/01_schema.sql
        sqlcmd reads the password from the SQLCMDPASSWORD environment variable,
        or prompts for it. The script is safe to re-run.

    Design notes:
        - ParcelId is an internal surrogate key. The ETL refreshes dbo.Parcel
          with TRUNCATE, which resets it, so it is not stable across loads and
          is never exposed by the API.
        - SourceObjectId is the City's OBJECTID and the public identifier. It
          is unique.
        - TasAcctId is not unique; source validation found one duplicate. It is
          not indexed because no query in this project looks parcels up by
          account.
        - ParcelGeometry is stored in the source projected CRS. The API
          reprojects to EPSG:4326 for the web.
*/

IF DB_ID(N'ReginaParcelExplorer') IS NULL
BEGIN
    CREATE DATABASE ReginaParcelExplorer;
END;
GO

USE ReginaParcelExplorer;
GO

IF OBJECT_ID(N'dbo.Parcel', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.Parcel
    (
        ParcelId        int IDENTITY(1,1) NOT NULL,
        SourceObjectId  int NOT NULL,
        TasAcctId       int NOT NULL,
        FullAddress     nvarchar(255) NOT NULL,
        AssessedValue   decimal(18,2) NOT NULL,
        MultiAccts      nvarchar(50) NULL,
        ParcelGeometry  geometry NOT NULL,
        LoadedAtUtc     datetime2(0) NOT NULL
            CONSTRAINT DF_Parcel_LoadedAtUtc
            DEFAULT SYSUTCDATETIME(),

        CONSTRAINT PK_Parcel
            PRIMARY KEY CLUSTERED (ParcelId),

        CONSTRAINT CK_Parcel_TasAcctId_Positive
            CHECK (TasAcctId > 0)
    );
END;
GO

-- Added separately so that re-running this script also upgrades an existing
-- database.
IF NOT EXISTS
(
    SELECT 1
    FROM sys.key_constraints
    WHERE name = N'UQ_Parcel_SourceObjectId'
      AND parent_object_id = OBJECT_ID(N'dbo.Parcel')
)
BEGIN
    ALTER TABLE dbo.Parcel
        ADD CONSTRAINT UQ_Parcel_SourceObjectId
            UNIQUE (SourceObjectId);
END;
GO

IF NOT EXISTS
(
    SELECT 1
    FROM sys.check_constraints
    WHERE name = N'CK_Parcel_ParcelGeometry_Srid'
      AND parent_object_id = OBJECT_ID(N'dbo.Parcel')
)
BEGIN
    ALTER TABLE dbo.Parcel
        ADD CONSTRAINT CK_Parcel_ParcelGeometry_Srid
            CHECK (ParcelGeometry.STSrid = 26913);
END;
GO

IF OBJECT_ID(N'dbo.EtlRun', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.EtlRun
    (
        EtlRunId             int IDENTITY(1,1) NOT NULL,
        StartedAtUtc         datetime2(0) NOT NULL,
        CompletedAtUtc       datetime2(0) NULL,
        SourceRecordCount    int NULL,
        LoadedRecordCount    int NULL,
        ExcludedRecordCount  int NULL,
        RunStatus            nvarchar(20) NOT NULL,
        ErrorMessage         nvarchar(4000) NULL,

        CONSTRAINT PK_EtlRun
            PRIMARY KEY CLUSTERED (EtlRunId),

        CONSTRAINT CK_EtlRun_RunStatus
            CHECK (RunStatus IN
                (N'Started', N'Succeeded', N'Failed'))
    );
END;
GO
