using NetTopologySuite.Geometries;
using ProjNet.CoordinateSystems.Transformations;
using ProjNet.SRID.Core;

namespace ReginaParcelExplorer.Api.Spatial;

/// <summary>
/// Converts between WGS84 (EPSG:4326, longitude/latitude) and the parcel
/// coordinate system (EPSG:26913, NAD83 / UTM zone 13N, metres).
/// Coordinates are passed as (longitude, latitude) and (x, y).
/// </summary>
public sealed class CoordinateTransformer
{
    public const int GeographicSrid = 4326;
    public const int ParcelSrid = 26913;

    // ProjNET does not document MathTransform thread safety, so access to the
    // shared transforms is serialised. The cost is negligible at this workload.
    private readonly Lock _gate = new();

    private readonly MathTransform _toParcel;
    private readonly MathTransform _toGeographic;

    public CoordinateTransformer()
    {
        var geographic = SRIDReader.GetCSbyID(GeographicSrid);
        var parcel = SRIDReader.GetCSbyID(ParcelSrid);

        var factory = new CoordinateTransformationFactory();

        _toParcel = factory
            .CreateFromCoordinateSystems(geographic, parcel)
            .MathTransform;

        _toGeographic = factory
            .CreateFromCoordinateSystems(parcel, geographic)
            .MathTransform;
    }

    public static bool IsValidLongitude(double value) =>
        double.IsFinite(value) && value is >= -180 and <= 180;

    public static bool IsValidLatitude(double value) =>
        double.IsFinite(value) && value is >= -90 and <= 90;

    public (double X, double Y) ToParcelCoordinates(
        double longitude,
        double latitude)
    {
        if (!IsValidLongitude(longitude))
        {
            throw new ArgumentOutOfRangeException(
                nameof(longitude),
                "Longitude must be between -180 and 180.");
        }

        if (!IsValidLatitude(latitude))
        {
            throw new ArgumentOutOfRangeException(
                nameof(latitude),
                "Latitude must be between -90 and 90.");
        }

        return Transform(_toParcel, longitude, latitude);
    }

    public (double Longitude, double Latitude) ToGeographicCoordinates(
        double x,
        double y)
    {
        return Transform(_toGeographic, x, y);
    }

    public Geometry ToGeographicGeometry(Geometry parcelGeometry)
    {
        ArgumentNullException.ThrowIfNull(parcelGeometry);

        if (parcelGeometry.SRID != ParcelSrid)
        {
            throw new ArgumentException(
                $"Expected geometry SRID {ParcelSrid}, " +
                $"but got {parcelGeometry.SRID}.",
                nameof(parcelGeometry));
        }

        var result = parcelGeometry.Copy();

        lock (_gate)
        {
            result.Apply(new MathTransformFilter(_toGeographic));
        }

        result.SRID = GeographicSrid;

        return result;
    }

    private (double X, double Y) Transform(
        MathTransform transform,
        double x,
        double y)
    {
        double[] result;

        lock (_gate)
        {
            result = transform.Transform(new[] { x, y });
        }

        if (!double.IsFinite(result[0]) || !double.IsFinite(result[1]))
        {
            throw new InvalidOperationException(
                "Coordinate transformation produced non-finite coordinates.");
        }

        return (result[0], result[1]);
    }

    private sealed class MathTransformFilter(MathTransform transform)
        : ICoordinateSequenceFilter
    {
        public bool Done => false;

        public bool GeometryChanged => true;

        public void Filter(CoordinateSequence sequence, int index)
        {
            var result = transform.Transform(
                new[] { sequence.GetX(index), sequence.GetY(index) });

            if (!double.IsFinite(result[0]) || !double.IsFinite(result[1]))
            {
                throw new InvalidOperationException(
                    "Transformation produced non-finite coordinates " +
                    $"at index {index}.");
            }

            sequence.SetX(index, result[0]);
            sequence.SetY(index, result[1]);
        }
    }
}
