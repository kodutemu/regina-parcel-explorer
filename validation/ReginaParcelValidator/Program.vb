Option Explicit On
Option Strict On
Option Infer On

Imports System.Data
Imports System.Threading.Tasks
Imports Microsoft.Data.SqlClient

' Validates the loaded dbo.Parcel table after etl/load_parcels.py has run.
' Pre-load validation of the raw extract is done by etl/validate_parcels.py.
'
' Configuration (environment variables, see .env.example):
'   SQL_SERVER, SQL_DATABASE, SQL_USER, SQL_PASSWORD
'   SQL_TRUST_SERVER_CERTIFICATE ("yes" or "no", default "no")
'
' Exit codes:
'   0  PASS or PASS_WITH_WARNINGS
'   1  FAIL
'   2  configuration or SQL error
Module Program
    Private Const ExpectedSrid As Integer = 26913
    Private Const CommandTimeoutSeconds As Integer = 60

    Private Const ValidationSql As String =
"SELECT
    COUNT_BIG(*) AS ParcelCount,
    COUNT_BIG(DISTINCT TasAcctId) AS DistinctAccountCount,
    COUNT_BIG(CASE WHEN SourceObjectId <= 0 THEN 1 END) AS BadSourceObjectIdCount,
    COUNT_BIG(CASE WHEN TasAcctId <= 0 THEN 1 END) AS BadTasAcctIdCount,
    COUNT_BIG(CASE WHEN FullAddress IS NULL OR TRIM(FullAddress) = '' THEN 1 END) AS MissingAddressCount,
    COUNT_BIG(CASE WHEN AssessedValue IS NULL OR AssessedValue < 0 THEN 1 END) AS BadAssessedValueCount,
    COUNT_BIG(CASE WHEN ParcelGeometry IS NULL THEN 1 END) AS NullGeometryCount,
    COUNT_BIG(CASE WHEN ParcelGeometry IS NOT NULL AND ParcelGeometry.STSrid <> @ExpectedSrid THEN 1 END) AS WrongSridCount,
    COUNT_BIG(CASE WHEN ParcelGeometry IS NOT NULL AND ParcelGeometry.STIsEmpty() = 1 THEN 1 END) AS EmptyGeometryCount,
    COUNT_BIG(CASE WHEN ParcelGeometry IS NOT NULL AND ParcelGeometry.STIsValid() <> 1 THEN 1 END) AS InvalidGeometryCount,
    COUNT_BIG(CASE WHEN ParcelGeometry IS NOT NULL AND ParcelGeometry.STGeometryType() NOT IN ('Polygon', 'MultiPolygon') THEN 1 END) AS UnexpectedGeometryTypeCount,
    (
        SELECT COUNT_BIG(*)
        FROM (
            SELECT TasAcctId
            FROM dbo.Parcel
            GROUP BY TasAcctId
            HAVING COUNT_BIG(*) > 1
        ) AS DuplicateAccounts
    ) AS DuplicateAccountIdCount,
    (
        SELECT TOP (1) LoadedRecordCount
        FROM dbo.EtlRun
        WHERE RunStatus = N'Succeeded'
        ORDER BY EtlRunId DESC
    ) AS LatestRunLoadedCount
FROM dbo.Parcel;"

    Private Class ValidationMetrics
        Public Property ParcelCount As Long
        Public Property DistinctAccountCount As Long
        Public Property BadSourceObjectIdCount As Long
        Public Property BadTasAcctIdCount As Long
        Public Property MissingAddressCount As Long
        Public Property BadAssessedValueCount As Long
        Public Property NullGeometryCount As Long
        Public Property WrongSridCount As Long
        Public Property EmptyGeometryCount As Long
        Public Property InvalidGeometryCount As Long
        Public Property UnexpectedGeometryTypeCount As Long
        Public Property DuplicateAccountIdCount As Long
        Public Property LatestRunLoadedCount As Long?
    End Class

    Public Function Main() As Integer
        Return MainAsync().GetAwaiter().GetResult()
    End Function

    Private Async Function MainAsync() As Task(Of Integer)
        Try
            Dim connectionString = BuildConnectionString()
            Dim metrics = Await ReadValidationMetricsAsync(connectionString)

            PrintResults(metrics)

            Return If(IsValidationPass(metrics), 0, 1)
        Catch ex As SqlException
            Console.Error.WriteLine($"SQL validation failed: {ex.Message}")
            Return 2
        Catch ex As Exception
            Console.Error.WriteLine($"Validation failed: {ex.Message}")
            Return 2
        End Try
    End Function

    Private Function RequireEnvironmentVariable(name As String) As String
        Dim value = Environment.GetEnvironmentVariable(name)

        If String.IsNullOrWhiteSpace(value) Then
            Throw New InvalidOperationException($"Missing environment variable {name}. See .env.example.")
        End If

        Return value
    End Function

    Private Function BuildConnectionString() As String
        Dim trustCertificate = If(Environment.GetEnvironmentVariable("SQL_TRUST_SERVER_CERTIFICATE"), "no").ToLowerInvariant()

        If trustCertificate <> "yes" AndAlso trustCertificate <> "no" Then
            Throw New InvalidOperationException("SQL_TRUST_SERVER_CERTIFICATE must be ""yes"" or ""no"".")
        End If

        Dim builder As New SqlConnectionStringBuilder With {
            .DataSource = RequireEnvironmentVariable("SQL_SERVER"),
            .InitialCatalog = RequireEnvironmentVariable("SQL_DATABASE"),
            .UserID = RequireEnvironmentVariable("SQL_USER"),
            .Password = RequireEnvironmentVariable("SQL_PASSWORD"),
            .Encrypt = SqlConnectionEncryptOption.Mandatory,
            .TrustServerCertificate = (trustCertificate = "yes"),
            .ConnectTimeout = 30
        }

        Return builder.ConnectionString
    End Function

    Private Async Function ReadValidationMetricsAsync(connectionString As String) As Task(Of ValidationMetrics)
        Using connection As New SqlConnection(connectionString)
            Await connection.OpenAsync()

            Using command As New SqlCommand(ValidationSql, connection)
                command.CommandTimeout = CommandTimeoutSeconds
                command.Parameters.Add("@ExpectedSrid", SqlDbType.Int).Value = ExpectedSrid

                Using reader = Await command.ExecuteReaderAsync()
                    If Not Await reader.ReadAsync() Then
                        Throw New InvalidOperationException("The validation query returned no result.")
                    End If

                    Return New ValidationMetrics With {
                        .ParcelCount = Convert.ToInt64(reader("ParcelCount")),
                        .DistinctAccountCount = Convert.ToInt64(reader("DistinctAccountCount")),
                        .BadSourceObjectIdCount = Convert.ToInt64(reader("BadSourceObjectIdCount")),
                        .BadTasAcctIdCount = Convert.ToInt64(reader("BadTasAcctIdCount")),
                        .MissingAddressCount = Convert.ToInt64(reader("MissingAddressCount")),
                        .BadAssessedValueCount = Convert.ToInt64(reader("BadAssessedValueCount")),
                        .NullGeometryCount = Convert.ToInt64(reader("NullGeometryCount")),
                        .WrongSridCount = Convert.ToInt64(reader("WrongSridCount")),
                        .EmptyGeometryCount = Convert.ToInt64(reader("EmptyGeometryCount")),
                        .InvalidGeometryCount = Convert.ToInt64(reader("InvalidGeometryCount")),
                        .UnexpectedGeometryTypeCount = Convert.ToInt64(reader("UnexpectedGeometryTypeCount")),
                        .DuplicateAccountIdCount = Convert.ToInt64(reader("DuplicateAccountIdCount")),
                        .LatestRunLoadedCount = ReadNullableCount(reader, "LatestRunLoadedCount")
                    }
                End Using
            End Using
        End Using
    End Function

    Private Function ReadNullableCount(reader As SqlDataReader, column As String) As Long?
        Dim ordinal = reader.GetOrdinal(column)

        If reader.IsDBNull(ordinal) Then
            Return Nothing
        End If

        Return Convert.ToInt64(reader.GetValue(ordinal))
    End Function

    Private Function RunMatchesTable(metrics As ValidationMetrics) As Boolean
        Return metrics.LatestRunLoadedCount.HasValue AndAlso
               metrics.LatestRunLoadedCount.Value = metrics.ParcelCount
    End Function

    Private Function IsValidationPass(metrics As ValidationMetrics) As Boolean
        Return metrics.ParcelCount > 0 AndAlso
               metrics.BadSourceObjectIdCount = 0 AndAlso
               metrics.BadTasAcctIdCount = 0 AndAlso
               metrics.MissingAddressCount = 0 AndAlso
               metrics.BadAssessedValueCount = 0 AndAlso
               metrics.NullGeometryCount = 0 AndAlso
               metrics.WrongSridCount = 0 AndAlso
               metrics.EmptyGeometryCount = 0 AndAlso
               metrics.InvalidGeometryCount = 0 AndAlso
               metrics.UnexpectedGeometryTypeCount = 0 AndAlso
               RunMatchesTable(metrics)
    End Function

    Private Function GetStatus(metrics As ValidationMetrics) As String
        If Not IsValidationPass(metrics) Then
            Return "FAIL"
        End If

        Return If(metrics.DuplicateAccountIdCount > 0, "PASS_WITH_WARNINGS", "PASS")
    End Function

    Private Sub PrintResults(metrics As ValidationMetrics)
        Console.WriteLine("Regina Parcel Validator")
        Console.WriteLine("=======================")
        Console.WriteLine($"Result: {GetStatus(metrics)}")
        Console.WriteLine()
        Console.WriteLine($"Parcel rows:                 {metrics.ParcelCount:N0}")
        Console.WriteLine($"Distinct account IDs:        {metrics.DistinctAccountCount:N0}")
        Console.WriteLine()

        PrintCheck("SourceObjectId values > 0", metrics.BadSourceObjectIdCount)
        PrintCheck("TAS_ACCT_ID values > 0", metrics.BadTasAcctIdCount)
        PrintCheck("Addresses missing or blank", metrics.MissingAddressCount)
        PrintCheck("Assessed values null or negative", metrics.BadAssessedValueCount)
        PrintCheck("Null parcel geometries", metrics.NullGeometryCount)
        PrintCheck("Parcel geometries with wrong SRID", metrics.WrongSridCount)
        PrintCheck("Empty parcel geometries", metrics.EmptyGeometryCount)
        PrintCheck("Invalid parcel geometries", metrics.InvalidGeometryCount)
        PrintCheck("Unexpected geometry types", metrics.UnexpectedGeometryTypeCount)
        PrintRunCheck(metrics)

        If metrics.DuplicateAccountIdCount > 0 Then
            Console.WriteLine($"WARNING  Duplicate TAS_ACCT_ID values: {metrics.DuplicateAccountIdCount:N0}")
        Else
            Console.WriteLine("PASS     Duplicate TAS_ACCT_ID values: 0")
        End If
    End Sub

    Private Sub PrintCheck(label As String, issueCount As Long)
        If issueCount = 0 Then
            Console.WriteLine($"PASS     {label}")
        Else
            Console.WriteLine($"FAIL     {label}: {issueCount:N0}")
        End If
    End Sub

    Private Sub PrintRunCheck(metrics As ValidationMetrics)
        Const label As String = "Parcel rows match latest successful ETL run"

        If RunMatchesTable(metrics) Then
            Console.WriteLine($"PASS     {label}")
        ElseIf metrics.LatestRunLoadedCount.HasValue Then
            Console.WriteLine($"FAIL     {label}: table {metrics.ParcelCount:N0}, run {metrics.LatestRunLoadedCount.Value:N0}")
        Else
            Console.WriteLine($"FAIL     {label}: no successful run recorded")
        End If
    End Sub
End Module
