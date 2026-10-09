using NetTopologySuite.Geometries;

namespace ReginaParcelExplorer.Api.Models;

public sealed record ParcelHitDto(
    int SourceObjectId,
    int TasAcctId,
    string FullAddress,
    decimal AssessedValue,
    string? MultiAccts,
    double AreaSquareMeters,
    Geometry Geometry);
