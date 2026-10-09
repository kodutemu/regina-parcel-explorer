namespace ReginaParcelExplorer.Api.Models;

public sealed record ParcelDto(
    int SourceObjectId,
    int TasAcctId,
    string FullAddress,
    decimal AssessedValue,
    string? MultiAccts,
    double AreaSquareMeters,
    double CentroidLongitude,
    double CentroidLatitude);
