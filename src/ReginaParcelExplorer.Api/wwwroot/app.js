const GeoJSONLayer = await $arcgis.import(
  "@arcgis/core/layers/GeoJSONLayer.js",
);

const LOCALE = "en-CA";

const mapElement = document.querySelector("#parcel-map");
const statusElement = document.querySelector("#query-status");
const resultsElement = document.querySelector("#parcel-results");

let highlightLayer = null;
let highlightUrl = null;
let activeQuery = null;

const highlightRenderer = {
  type: "simple",
  symbol: {
    type: "simple-fill",
    color: [255, 193, 7, 0.15],
    outline: {
      color: [255, 193, 7, 1],
      width: 2,
    },
  },
};

await mapElement.viewOnReady();

mapElement.addEventListener("arcgisViewClick", async (event) => {
  const { longitude, latitude } = event.detail.mapPoint;

  // Only the most recent click may update the page.
  activeQuery?.abort();
  const query = new AbortController();
  activeQuery = query;
  const isCurrent = () => activeQuery === query;

  resultsElement.replaceChildren();
  clearParcelHighlight();

  statusElement.textContent = `Querying ${longitude.toFixed(6)}, ${latitude.toFixed(6)}...`;

  try {
    const parcels = await fetchParcelsAtPoint(
      longitude,
      latitude,
      query.signal,
    );

    if (!isCurrent()) {
      return;
    }

    if (parcels.length === 0) {
      statusElement.textContent =
        "No parcels were found at the clicked location.";
      return;
    }

    const countMessage = `${parcels.length} ${parcels.length === 1 ? "parcel" : "parcels"} found`;

    statusElement.textContent = `${countMessage}.`;
    resultsElement.append(...parcels.map(createParcelResult));

    try {
      await displayParcelsOnMap(parcels);
    } catch (error) {
      if (!isCurrent()) {
        return;
      }

      console.error("Parcel geometry rendering failed.", error);
      clearParcelHighlight();

      statusElement.textContent = `${countMessage}, but the map highlight could not be displayed.`;
    }
  } catch (error) {
    if (error.name === "AbortError" || !isCurrent()) {
      return;
    }

    console.error(error);

    statusElement.textContent = "The parcel query could not be completed.";
  }
});

async function fetchParcelsAtPoint(longitude, latitude, signal) {
  const params = new URLSearchParams({ longitude, latitude });

  const response = await fetch(`/api/parcels/at-point?${params}`, { signal });

  if (!response.ok) {
    throw new Error(`Parcel query failed with HTTP ${response.status}.`);
  }

  return response.json();
}

async function displayParcelsOnMap(parcels) {
  const featureCollection = {
    type: "FeatureCollection",
    features: parcels.map((parcel) => ({
      type: "Feature",
      id: parcel.sourceObjectId,
      properties: {
        sourceObjectId: parcel.sourceObjectId,
        tasAcctId: parcel.tasAcctId,
        fullAddress: parcel.fullAddress,
      },
      geometry: parcel.geometry,
    })),
  };

  const blob = new Blob([JSON.stringify(featureCollection)], {
    type: "application/json",
  });

  highlightUrl = URL.createObjectURL(blob);

  highlightLayer = new GeoJSONLayer({
    url: highlightUrl,
    title: "Selected parcels",
    renderer: highlightRenderer,
  });

  mapElement.map.add(highlightLayer);

  await highlightLayer.load();
}

function clearParcelHighlight() {
  if (highlightLayer) {
    mapElement.map.remove(highlightLayer);
    highlightLayer.destroy();
    highlightLayer = null;
  }

  if (highlightUrl) {
    URL.revokeObjectURL(highlightUrl);
    highlightUrl = null;
  }
}

function createParcelResult(parcel) {
  const container = document.createElement("article");
  container.className = "parcel-result";

  const address = document.createElement("strong");
  address.textContent = parcel.fullAddress;

  container.append(
    address,
    createDetail("Account ID", String(parcel.tasAcctId)),
    createDetail("Assessed value", formatCurrency(parcel.assessedValue)),
    createDetail("Area", formatArea(parcel.areaSquareMeters)),
  );

  return container;
}

function createDetail(label, value) {
  const element = document.createElement("p");
  element.className = "parcel-detail";
  element.textContent = `${label}: ${value}`;
  return element;
}

function formatCurrency(value) {
  return new Intl.NumberFormat(LOCALE, {
    style: "currency",
    currency: "CAD",
    maximumFractionDigits: 0,
  }).format(value);
}

function formatArea(squareMeters) {
  return `${squareMeters.toLocaleString(LOCALE, { maximumFractionDigits: 2 })} m²`;
}
