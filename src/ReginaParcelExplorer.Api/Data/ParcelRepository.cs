using System.Data;
using Microsoft.Data.SqlClient;
using NetTopologySuite.IO;
using ReginaParcelExplorer.Api.Models;
using ReginaParcelExplorer.Api.Spatial;

namespace ReginaParcelExplorer.Api.Data;

public sealed class ParcelRepository(
    string connectionString,
    CoordinateTransformer coordinateTransformer)
{
    public async Task<ParcelDto?> GetBySourceObjectIdAsync(
        int sourceObjectId,
        CancellationToken cancellationToken)
    {
        const string sql =
            """
            SELECT
                SourceObjectId,
                TasAcctId,
                FullAddress,
                AssessedValue,
                MultiAccts,
                ParcelGeometry.STArea() AS AreaSquareMeters,
                ParcelGeometry.STCentroid().STX AS CentroidX,
                ParcelGeometry.STCentroid().STY AS CentroidY
            FROM dbo.Parcel
            WHERE SourceObjectId = @SourceObjectId;
            """;

        await using var connection = new SqlConnection(connectionString);
        await connection.OpenAsync(cancellationToken);

        await using var command = new SqlCommand(sql, connection);
        command.Parameters.Add(
            new SqlParameter("@SourceObjectId", SqlDbType.Int)
            {
                Value = sourceObjectId
            });

        await using var reader =
            await command.ExecuteReaderAsync(cancellationToken);

        if (!await reader.ReadAsync(cancellationToken))
        {
            return null;
        }

        var row = ReadAttributes(reader);

        var (centroidLongitude, centroidLatitude) =
            coordinateTransformer.ToGeographicCoordinates(
                Get<double>(reader, "CentroidX"),
                Get<double>(reader, "CentroidY"));

        return new ParcelDto(
            row.SourceObjectId,
            row.TasAcctId,
            row.FullAddress,
            row.AssessedValue,
            row.MultiAccts,
            row.AreaSquareMeters,
            centroidLongitude,
            centroidLatitude);
    }

    public async Task<IReadOnlyList<ParcelHitDto>> FindAtPointAsync(
        double x,
        double y,
        CancellationToken cancellationToken)
    {
        const string sql =
            """
            DECLARE @Point geometry = geometry::Point(@X, @Y, @Srid);

            SELECT
                SourceObjectId,
                TasAcctId,
                FullAddress,
                AssessedValue,
                MultiAccts,
                ParcelGeometry.STArea() AS AreaSquareMeters,
                ParcelGeometry.STAsBinary() AS GeometryWkb
            FROM dbo.Parcel
            WHERE ParcelGeometry.STIntersects(@Point) = 1
            ORDER BY SourceObjectId;
            """;

        await using var connection = new SqlConnection(connectionString);
        await connection.OpenAsync(cancellationToken);

        await using var command = new SqlCommand(sql, connection);
        command.Parameters.Add(
            new SqlParameter("@X", SqlDbType.Float) { Value = x });
        command.Parameters.Add(
            new SqlParameter("@Y", SqlDbType.Float) { Value = y });
        command.Parameters.Add(
            new SqlParameter("@Srid", SqlDbType.Int)
            {
                Value = CoordinateTransformer.ParcelSrid
            });

        await using var reader =
            await command.ExecuteReaderAsync(cancellationToken);

        var wkbReader = new WKBReader();
        var results = new List<ParcelHitDto>();

        while (await reader.ReadAsync(cancellationToken))
        {
            var row = ReadAttributes(reader);

            var parcelGeometry =
                wkbReader.Read(Get<byte[]>(reader, "GeometryWkb"));
            parcelGeometry.SRID = CoordinateTransformer.ParcelSrid;

            results.Add(new ParcelHitDto(
                row.SourceObjectId,
                row.TasAcctId,
                row.FullAddress,
                row.AssessedValue,
                row.MultiAccts,
                row.AreaSquareMeters,
                coordinateTransformer.ToGeographicGeometry(parcelGeometry)));
        }

        return results;
    }

    private readonly record struct ParcelAttributes(
        int SourceObjectId,
        int TasAcctId,
        string FullAddress,
        decimal AssessedValue,
        string? MultiAccts,
        double AreaSquareMeters);

    private static ParcelAttributes ReadAttributes(SqlDataReader reader) =>
        new(
            Get<int>(reader, "SourceObjectId"),
            Get<int>(reader, "TasAcctId"),
            Get<string>(reader, "FullAddress"),
            Get<decimal>(reader, "AssessedValue"),
            GetNullableString(reader, "MultiAccts"),
            Get<double>(reader, "AreaSquareMeters"));

    private static T Get<T>(SqlDataReader reader, string column) =>
        reader.GetFieldValue<T>(reader.GetOrdinal(column));

    private static string? GetNullableString(
        SqlDataReader reader,
        string column)
    {
        var ordinal = reader.GetOrdinal(column);
        return reader.IsDBNull(ordinal) ? null : reader.GetString(ordinal);
    }
}
