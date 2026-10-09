using Microsoft.AspNetCore.Http.HttpResults;
using NetTopologySuite.IO.Converters;
using ReginaParcelExplorer.Api.Data;
using ReginaParcelExplorer.Api.Models;
using ReginaParcelExplorer.Api.Spatial;

const string ConnectionStringName = "ReginaParcelExplorer";

var builder = WebApplication.CreateBuilder(args);

var connectionString =
    builder.Configuration.GetConnectionString(ConnectionStringName)
    ?? throw new InvalidOperationException(
        $"Connection string '{ConnectionStringName}' is not configured. " +
        "Set it with dotnet user-secrets or the " +
        $"ConnectionStrings__{ConnectionStringName} environment variable.");

builder.Services.AddOpenApi();
builder.Services.AddProblemDetails();

builder.Services.ConfigureHttpJsonOptions(options =>
    options.SerializerOptions.Converters.Add(new GeoJsonConverterFactory()));

builder.Services.AddSingleton<CoordinateTransformer>();
builder.Services.AddSingleton(services => new ParcelRepository(
    connectionString,
    services.GetRequiredService<CoordinateTransformer>()));

var app = builder.Build();

if (app.Environment.IsDevelopment())
{
    app.MapOpenApi();
}
else
{
    app.UseExceptionHandler();
    app.UseStatusCodePages();
}

// MapStaticAssets does not serve default documents on its own, so the
// default-file rewrite and static file middleware are kept for "/".
app.UseDefaultFiles();
app.UseStaticFiles();
app.MapStaticAssets();

var parcels = app.MapGroup("/api/parcels").WithTags("Parcels");

parcels.MapGet(
        "/{sourceObjectId:int}",
        async Task<Results<Ok<ParcelDto>, NotFound, ValidationProblem>> (
            int sourceObjectId,
            ParcelRepository repository,
            CancellationToken cancellationToken) =>
        {
            if (sourceObjectId <= 0)
            {
                return TypedResults.ValidationProblem(
                    new Dictionary<string, string[]>
                    {
                        ["sourceObjectId"] =
                            ["sourceObjectId must be greater than zero."]
                    });
            }

            var parcel = await repository.GetBySourceObjectIdAsync(
                sourceObjectId,
                cancellationToken);

            if (parcel is null)
            {
                return TypedResults.NotFound();
            }

            return TypedResults.Ok(parcel);
        })
    .WithName("GetParcel")
    .WithSummary("Get a parcel by its source (City of Regina) object ID.");

parcels.MapGet(
        "/at-point",
        async Task<Results<Ok<IReadOnlyList<ParcelHitDto>>, ValidationProblem>> (
            double? longitude,
            double? latitude,
            CoordinateTransformer transformer,
            ParcelRepository repository,
            CancellationToken cancellationToken) =>
        {
            var lon = longitude ?? double.NaN;
            var lat = latitude ?? double.NaN;

            var errors = new Dictionary<string, string[]>();

            if (!CoordinateTransformer.IsValidLongitude(lon))
            {
                errors["longitude"] =
                    ["longitude is required and must be between -180 and 180."];
            }

            if (!CoordinateTransformer.IsValidLatitude(lat))
            {
                errors["latitude"] =
                    ["latitude is required and must be between -90 and 90."];
            }

            if (errors.Count > 0)
            {
                return TypedResults.ValidationProblem(errors);
            }

            var (x, y) = transformer.ToParcelCoordinates(lon, lat);

            var hits = await repository.FindAtPointAsync(
                x,
                y,
                cancellationToken);

            return TypedResults.Ok(hits);
        })
    .WithName("FindParcelsAtPoint")
    .WithSummary(
        "Find parcels intersecting a WGS84 longitude/latitude point. " +
        "Returns an empty list when the point is not inside a parcel.");

app.Run();
