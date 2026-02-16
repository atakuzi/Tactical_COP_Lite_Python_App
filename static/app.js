const STALE_SEC = 90;

const statusEl = document.getElementById("status");
const tracklistEl = document.getElementById("tracklist");
const mjpegEl = document.getElementById("mjpeg");
const fpvSelectEl = document.getElementById("fpv-select");
const fpvListEl = document.getElementById("fpv-list");

const MAPBOX_TOKEN = window.MAPBOX_ACCESS_TOKEN || "";
const MAPBOX_STYLE = window.MAPBOX_STYLE || "mapbox://styles/mapbox/dark-v11";

let map = null;
let glLib = null;
let mapEngine = null;
let leafletLayers = null;

const OSM_RASTER_FALLBACK_STYLE = {
  version: 8,
  sources: {
    "osm-tiles": {
      type: "raster",
      tiles: [
        "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "https://b.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "https://c.tile.openstreetmap.org/{z}/{x}/{y}.png",
      ],
      tileSize: 256,
      attribution: "(c) OpenStreetMap contributors",
    },
  },
  layers: [
    {
      id: "osm-base",
      type: "raster",
      source: "osm-tiles",
      minzoom: 0,
      maxzoom: 19,
    },
  ],
};

const layerVisibility = {
  friendly: true,
  enemy: true,
  fires: true,
  air: true,
  ew: true,
  other: true,
};

const markersByUid = new Map();

const AFFILIATION = { friendly: "F", enemy: "H", neutral: "N", unknown: "U" };

const LAYER_SIDC = {
  friendly: "S*GPUCI---*****",
  enemy: "S*GPUCI---*****",
  fires: "S*GPUCF---*****",
  air: "S*APMFQ---*****",
  ew: "S*GPEWM---*****",
  other: "S*GP------*****",
};

const SYM_SIZE = 40;
const DEFAULT_VIDEO_STREAM = "/video/mjpeg";
const fpvState = {
  selectedStream: DEFAULT_VIDEO_STREAM,
  drones: [],
  byUid: new Map(),
};
let sseConnected = false;
let sseRetryTimer = null;

function initMap() {
  const clearMarkerStates = () => {
    // Markers are bound to a specific map instance; force re-attach after engine/map swaps.
    for (const markerState of markersByUid.values()) {
      markerState.visible = false;
      if (markerState.groupName) markerState.groupName = markerState.layer || markerState.groupName;
    }
  };

  const initIframeFallback = (reason) => {
    const mapEl = document.getElementById("map");
    if (!mapEl) {
      statusEl.textContent = `Map failed to initialize (${reason})`;
      return;
    }
    mapEngine = "iframe";
    map = null;
    mapEl.innerHTML = "";
    const iframe = document.createElement("iframe");
    iframe.src = "https://www.openstreetmap.org/export/embed.html?bbox=5.5%2C47.0%2C15.5%2C53.5&layer=mapnik";
    iframe.title = "Map fallback";
    iframe.style.width = "100%";
    iframe.style.height = "100%";
    iframe.style.border = "0";
    iframe.loading = "lazy";
    mapEl.appendChild(iframe);
    statusEl.textContent = `Map initialized with iframe fallback (${reason})`;
  };

  const initLeafletFallback = (reason) => {
    if (!window.L) {
      initIframeFallback(`${reason}; Leaflet unavailable`);
      return;
    }
    mapEngine = "leaflet";
    map = L.map("map", { zoomControl: true }).setView([50.1109, 8.6821], 6);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "(c) OpenStreetMap contributors",
    }).addTo(map);
    leafletLayers = {
      friendly: L.layerGroup().addTo(map),
      enemy: L.layerGroup().addTo(map),
      fires: L.layerGroup().addTo(map),
      air: L.layerGroup().addTo(map),
      ew: L.layerGroup().addTo(map),
      other: L.layerGroup().addTo(map),
    };
    statusEl.textContent = `Map initialized with Leaflet fallback (${reason})`;
  };

  const hasMapboxRuntime = !!window.mapboxgl;
  const hasMapLibreRuntime = !!window.maplibregl;

  // Prefer Mapbox only when a token is available; otherwise prefer MapLibre for token-free rendering.
  if (MAPBOX_TOKEN && hasMapboxRuntime) {
    glLib = window.mapboxgl;
    window.mapboxgl.accessToken = MAPBOX_TOKEN;
  } else if (hasMapLibreRuntime) {
    glLib = window.maplibregl;
  } else if (hasMapboxRuntime) {
    glLib = window.mapboxgl;
  } else {
    glLib = null;
  }

  if (!glLib) {
    initLeafletFallback("WebGL engine unavailable");
    return;
  }

  const usingToken = glLib === window.mapboxgl && !!MAPBOX_TOKEN;

  try {
    mapEngine = "gl";
    map = new glLib.Map({
      container: "map",
      style: usingToken ? MAPBOX_STYLE : OSM_RASTER_FALLBACK_STYLE,
      center: [8.6821, 50.1109],
      zoom: 6,
    });
    map.addControl(new glLib.NavigationControl(), "top-right");
  } catch (e) {
    map = null;
    mapEngine = null;
    initLeafletFallback("WebGL initialization failed");
    clearMarkerStates();
    return;
  }

  let loaded = false;
  map.once("load", () => {
    loaded = true;
  });

  if (usingToken) {
    let downgraded = false;
    const downgradeToOsm = (reason) => {
      if (downgraded || !map) return;
      downgraded = true;
      const center = map.getCenter();
      const zoom = map.getZoom();
      map.remove();
      map = new glLib.Map({
        container: "map",
        style: OSM_RASTER_FALLBACK_STYLE,
        center: [center.lng, center.lat],
        zoom,
      });
      map.addControl(new glLib.NavigationControl(), "top-right");
      clearMarkerStates();
      statusEl.textContent = `Mapbox style unavailable; using OpenStreetMap fallback (${reason})`;
      refresh();
    };

    map.on("error", () => {
      if (!loaded) downgradeToOsm("style error");
    });
    setTimeout(() => {
      if (!loaded) downgradeToOsm("timeout");
    }, 6000);
  } else {
    statusEl.textContent = "Map initialized with OpenStreetMap fallback";
  }
}

function iconFor(track) {
  const aff = AFFILIATION[track.side] || "U";
  const sidcTemplate = track.meta?.sidc || LAYER_SIDC[track.layer] || LAYER_SIDC.other;
  const sidc = sidcTemplate[0] + aff + sidcTemplate.slice(2);

  const sym = new ms.Symbol(sidc, { size: SYM_SIZE });
  const size = sym.getSize();
  const anchor = sym.getAnchor();

  return {
    svg: sym.asSVG(),
    offset: [Math.round(size.width / 2 - anchor.x), Math.round(size.height / 2 - anchor.y)],
  };
}

function setMarker(track) {
  if (!map) return;

  const layerName = track.layer || "other";
  const markerState = markersByUid.get(track.uid);
  const icon = iconFor(track);
  const popupHtml = `<b>${track.meta?.callsign || track.uid}</b><br/>${track.side} | ${layerName}<br/>Updated: ${new Date(track.updated_at).toLocaleString()}`;

  if (mapEngine === "leaflet") {
    const group = (leafletLayers && leafletLayers[layerName]) || (leafletLayers && leafletLayers.other);
    const leafletIcon = L.divIcon({
      className: "cop-marker",
      html: icon.svg,
      iconSize: [SYM_SIZE, SYM_SIZE],
      iconAnchor: [SYM_SIZE / 2, SYM_SIZE / 2],
      popupAnchor: [0, -SYM_SIZE / 2],
    });

    if (!markerState) {
      const marker = L.marker([track.lat, track.lon], { icon: leafletIcon }).bindPopup(popupHtml);
      const state = { marker, layer: layerName, groupName: layerName, visible: false, engine: "leaflet" };
      markersByUid.set(track.uid, state);
      applyMarkerVisibility(state, layerName);
      return;
    }

    markerState.layer = layerName;
    markerState.marker.setLatLng([track.lat, track.lon]);
    markerState.marker.setIcon(leafletIcon);
    markerState.marker.bindPopup(popupHtml);
    applyMarkerVisibility(markerState, layerName);
    return;
  }

  if (!markerState) {
    const el = document.createElement("div");
    el.className = "cop-marker";
    el.innerHTML = icon.svg;

    const marker = new glLib.Marker({ element: el, offset: icon.offset })
      .setLngLat([track.lon, track.lat])
      .setPopup(new glLib.Popup({ offset: 12 }).setHTML(popupHtml));

    const state = { marker, element: el, layer: layerName, visible: false };
    markersByUid.set(track.uid, state);
    applyMarkerVisibility(state, layerName);
    return;
  }

  markerState.layer = layerName;
  markerState.element.innerHTML = icon.svg;
  markerState.marker.setOffset(icon.offset);
  markerState.marker.setLngLat([track.lon, track.lat]);
  markerState.marker.setPopup(new glLib.Popup({ offset: 12 }).setHTML(popupHtml));
  applyMarkerVisibility(markerState, layerName);
}

function applyMarkerVisibility(markerState, layerName) {
  if (!map) return;
  const shouldShow = layerVisibility[layerName] !== false;
  if (mapEngine === "leaflet" && markerState.engine === "leaflet") {
    const targetGroup = (leafletLayers && leafletLayers[layerName]) || (leafletLayers && leafletLayers.other);
    const currentGroup =
      (leafletLayers && leafletLayers[markerState.groupName]) ||
      targetGroup;

    if (shouldShow && !markerState.visible) {
      markerState.marker.addTo(targetGroup);
      markerState.visible = true;
      markerState.groupName = layerName;
    } else if (shouldShow && markerState.visible && markerState.groupName !== layerName) {
      currentGroup.removeLayer(markerState.marker);
      markerState.marker.addTo(targetGroup);
      markerState.groupName = layerName;
    } else if (!shouldShow && markerState.visible) {
      currentGroup.removeLayer(markerState.marker);
      markerState.visible = false;
      markerState.groupName = layerName;
    }
    return;
  }
  if (shouldShow && !markerState.visible) {
    markerState.marker.addTo(map);
    markerState.visible = true;
  } else if (!shouldShow && markerState.visible) {
    markerState.marker.remove();
    markerState.visible = false;
  }
}

function applyAllLayerVisibility() {
  for (const markerState of markersByUid.values()) {
    applyMarkerVisibility(markerState, markerState.layer);
  }
}

function flyToPoint(lat, lon, minZoom = 12) {
  if (!map) return;
  if (mapEngine === "leaflet") {
    map.flyTo([lat, lon], Math.max(map.getZoom(), minZoom));
    return;
  }
  map.flyTo({ center: [lon, lat], zoom: Math.max(map.getZoom(), minZoom) });
}

function updateTrackList(tracks, serverTimeIso) {
  const now = new Date(serverTimeIso);
  tracklistEl.innerHTML = "";
  for (const t of tracks) {
    const updated = new Date(t.updated_at);
    const ageSec = Math.max(0, (now - updated) / 1000.0);
    const stale = ageSec > STALE_SEC;

    const div = document.createElement("button");
    div.type = "button";
    div.className = `bp5-card bp5-elevation-0 track${stale ? " stale" : ""}`;

    const cs = t.meta?.callsign || t.uid;
    div.innerHTML = `
      <div class="track-row">
        <strong>${cs}</strong>
        <span class="bp5-tag bp5-minimal track-tag-${t.side}">${t.side}</span>
      </div>
      <div class="bp5-text-muted track-meta">${t.layer} | age ${ageSec.toFixed(0)}s</div>
      <div class="bp5-text-muted track-meta">${t.lat.toFixed(4)}, ${t.lon.toFixed(4)}</div>
    `;
    div.addEventListener("click", () => {
      flyToPoint(t.lat, t.lon, 12);
    });
    tracklistEl.appendChild(div);
  }
}

function reconcileMarkers(tracks) {
  const liveUids = new Set(tracks.map((t) => t.uid));
  for (const [uid, markerState] of markersByUid.entries()) {
    if (liveUids.has(uid)) continue;
    if (mapEngine === "leaflet" && markerState.engine === "leaflet") {
      const group =
        (leafletLayers && leafletLayers[markerState.groupName]) ||
        (leafletLayers && leafletLayers.other);
      if (group && markerState.visible) {
        group.removeLayer(markerState.marker);
      }
    } else {
      markerState.marker.remove();
    }
    markersByUid.delete(uid);
  }
}

function applyTracksPayload(data, transport = "poll") {
  const tracks = data.tracks || [];
  const serverTime = data.server_time || new Date().toISOString();
  statusEl.textContent = `Tracks: ${tracks.length} | ${transport.toUpperCase()} | Server: ${new Date(serverTime).toLocaleTimeString()}`;
  for (const t of tracks) setMarker(t);
  reconcileMarkers(tracks);
  updateTrackList(tracks, serverTime);
}

async function refresh() {
  try {
    const res = await fetch("/api/tracks");
    const data = await res.json();
    applyTracksPayload(data, "poll");
  } catch (e) {
    statusEl.textContent = "Disconnected - showing last known positions";
  }
}

function connectTrackStream() {
  if (!window.EventSource) {
    statusEl.textContent = "EventSource unsupported - polling mode";
    setInterval(refresh, 1500);
    refresh();
    return;
  }

  const es = new EventSource("/api/tracks/stream");
  es.addEventListener("tracks", (ev) => {
    try {
      const data = JSON.parse(ev.data);
      applyTracksPayload(data, "live");
      sseConnected = true;
      if (sseRetryTimer) {
        clearTimeout(sseRetryTimer);
        sseRetryTimer = null;
      }
    } catch (e) {
      // Ignore bad event payload and keep stream alive.
    }
  });

  es.onerror = () => {
    if (sseConnected) {
      statusEl.textContent = "Live stream interrupted - retrying";
    }
    es.close();
    sseConnected = false;
    if (!sseRetryTimer) {
      sseRetryTimer = setTimeout(() => {
        sseRetryTimer = null;
        connectTrackStream();
      }, 2500);
    }
    refresh();
  };
}

function syncStreamSelection() {
  const desired = fpvSelectEl.value || DEFAULT_VIDEO_STREAM;
  if (mjpegEl.getAttribute("src") !== desired) {
    mjpegEl.setAttribute("src", desired);
  }
  fpvState.selectedStream = desired;
}

function renderFpvControls() {
  const current = fpvState.selectedStream || DEFAULT_VIDEO_STREAM;
  fpvSelectEl.innerHTML = "";

  const primaryOpt = document.createElement("option");
  primaryOpt.value = DEFAULT_VIDEO_STREAM;
  primaryOpt.textContent = "Primary FMV";
  fpvSelectEl.appendChild(primaryOpt);

  for (const d of fpvState.drones) {
    const opt = document.createElement("option");
    opt.value = d.stream_url;
    opt.textContent = `${d.callsign} (${d.uid})`;
    fpvSelectEl.appendChild(opt);
  }

  const validStreams = new Set([DEFAULT_VIDEO_STREAM, ...fpvState.drones.map((d) => d.stream_url)]);
  fpvSelectEl.value = validStreams.has(current) ? current : DEFAULT_VIDEO_STREAM;
  syncStreamSelection();

  fpvListEl.innerHTML = "";
  for (const d of fpvState.drones) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "bp5-button bp5-small fpv-chip";
    btn.textContent = d.callsign;
    btn.addEventListener("click", () => {
      fpvSelectEl.value = d.stream_url;
      syncStreamSelection();
      flyToPoint(d.lat, d.lon, 12);
    });
    fpvListEl.appendChild(btn);
  }
}

async function refreshFpvDrones() {
  try {
    const res = await fetch("/api/fpv/drones");
    if (!res.ok) return;
    const data = await res.json();
    const drones = data.enabled ? (data.drones || []) : [];
    fpvState.drones = drones;
    fpvState.byUid = new Map(drones.map((d) => [d.uid, d]));
    renderFpvControls();
  } catch (e) {
    // FPV simulation is optional.
  }
}

initMap();
connectTrackStream();
setInterval(refreshFpvDrones, 3000);
refreshFpvDrones();

document.querySelectorAll('input[type="checkbox"][data-layer]').forEach((cb) => {
  cb.addEventListener("change", () => {
    const name = cb.getAttribute("data-layer");
    layerVisibility[name] = cb.checked;
    applyAllLayerVisibility();
  });
});

document.getElementById("btn-center").addEventListener("click", () => {
  if (!map) return;
  if (mapEngine === "leaflet") {
    map.flyTo([50.1109, 8.6821], 6);
    return;
  }
  map.flyTo({ center: [8.6821, 50.1109], zoom: 6 });
});

document.getElementById("btn-demo").addEventListener("click", async () => {
  const demo = [
    { uid: "FRD-ALPHA1", side: "friendly", layer: "friendly", lat: 50.1109, lon: 8.6821, meta: { callsign: "ALPHA 1", sidc: "SFGPUCI---D*****" } },
    { uid: "FRD-BRAVO2", side: "friendly", layer: "friendly", lat: 52.52, lon: 13.405, meta: { callsign: "BRAVO 2", sidc: "SFGPUCA---D*****" } },
    { uid: "FRD-CHARLIE3", side: "friendly", layer: "friendly", lat: 50.94, lon: 6.9578, meta: { callsign: "CHARLIE 3", sidc: "SFGPUCIM--D*****" } },
    { uid: "FRD-DELTA4", side: "friendly", layer: "friendly", lat: 49.0069, lon: 8.4037, meta: { callsign: "DELTA 4", sidc: "SFGPUCRVA-D*****" } },
    { uid: "FRD-ECHO5", side: "friendly", layer: "friendly", lat: 51.2277, lon: 6.7735, meta: { callsign: "ECHO 5", sidc: "SFGPUCI---C*****" } },
    { uid: "FRD-FOXTRT6", side: "friendly", layer: "friendly", lat: 50.3569, lon: 7.589, meta: { callsign: "FOXTROT 6", sidc: "SFGPUCE---D*****" } },
    { uid: "FRD-HQ-BDE", side: "friendly", layer: "friendly", lat: 50.5861, lon: 8.6743, meta: { callsign: "WARHORSE 6", sidc: "SFGPUH----F*****" } },
    { uid: "FRD-BN-HQ1", side: "friendly", layer: "friendly", lat: 50.7753, lon: 9.1802, meta: { callsign: "IRON 6", sidc: "SFGPUH----E*****" } },
    { uid: "FRD-SUPPLY1", side: "friendly", layer: "friendly", lat: 49.8728, lon: 8.6512, meta: { callsign: "BLACKHORSE LOG", sidc: "SFGPUSS---D*****" } },
    { uid: "FRD-MED1", side: "friendly", layer: "friendly", lat: 49.7913, lon: 9.9356, meta: { callsign: "MEDEVAC 1", sidc: "SFGPUSM---C*****" } },
    { uid: "FRD-MP1", side: "friendly", layer: "friendly", lat: 50.0782, lon: 8.2398, meta: { callsign: "GUARDIAN 1", sidc: "SFGPUSL---C*****" } },
    { uid: "FRD-SIG1", side: "friendly", layer: "friendly", lat: 50.4119, lon: 9.4078, meta: { callsign: "SIGNAL 6", sidc: "SFGPUUS---D*****" } },
    { uid: "FIRES-BTRY1", side: "friendly", layer: "fires", lat: 49.4521, lon: 11.0767, meta: { callsign: "STEEL RAIN", sidc: "SFGPUCFHE-D*****" } },
    { uid: "FIRES-BTRY2", side: "friendly", layer: "fires", lat: 50.6821, lon: 10.2311, meta: { callsign: "THUNDER", sidc: "SFGPUCFHE-D*****" } },
    { uid: "FIRES-MLRS1", side: "friendly", layer: "fires", lat: 49.9137, lon: 10.8865, meta: { callsign: "KING OF BATTLE", sidc: "SFGPUCFR--D*****" } },
    { uid: "FIRES-MTR1", side: "friendly", layer: "fires", lat: 50.2644, lon: 11.3941, meta: { callsign: "HELLFIRE", sidc: "SFGPUCFM--C*****" } },
    { uid: "AIR-UAS1", side: "friendly", layer: "air", lat: 48.1351, lon: 11.582, meta: { callsign: "SHADOW 6", sidc: "SFAPMFQ---*****" } },
    { uid: "AIR-UAS2", side: "friendly", layer: "air", lat: 51.4556, lon: 7.0116, meta: { callsign: "RAVEN 3", sidc: "SFAPMFQ---*****" } },
    { uid: "AIR-ROTARY1", side: "friendly", layer: "air", lat: 49.87, lon: 8.92, meta: { callsign: "DUSTOFF 9", sidc: "SFAPMHA---*****" } },
    { uid: "AIR-ROTARY2", side: "friendly", layer: "air", lat: 50.8667, lon: 7.1431, meta: { callsign: "REAPER 2", sidc: "SFAPMHA---*****" } },
    { uid: "AIR-MEDEVAC1", side: "friendly", layer: "air", lat: 49.4875, lon: 8.466, meta: { callsign: "DUSTOFF 1", sidc: "SFAPMHU---*****" } },
    { uid: "EW-TEAM1", side: "friendly", layer: "ew", lat: 53.5511, lon: 9.9937, meta: { callsign: "SPECTRE", sidc: "SFGPEWM---C*****" } },
    { uid: "EW-TEAM2", side: "friendly", layer: "ew", lat: 51.9607, lon: 7.6261, meta: { callsign: "PHANTOM", sidc: "SFGPEWD---C*****" } },
    { uid: "ENY-RED1", side: "enemy", layer: "enemy", lat: 51.0504, lon: 13.7373, meta: { callsign: "RED 1", sidc: "SHGPUCIM--E*****" } },
    { uid: "ENY-RED2", side: "enemy", layer: "enemy", lat: 50.93, lon: 14.12, meta: { callsign: "RED 2", sidc: "SHGPUCA---D*****" } },
    { uid: "ENY-RED3", side: "enemy", layer: "enemy", lat: 51.3397, lon: 12.3731, meta: { callsign: "RED 3", sidc: "SHGPUCI---E*****" } },
    { uid: "ENY-RED-ARTY", side: "enemy", layer: "enemy", lat: 51.1657, lon: 14.971, meta: { callsign: "RED ARTY", sidc: "SHGPUCFHE-E*****" } },
    { uid: "NEU-OBS1", side: "neutral", layer: "other", lat: 51.5, lon: 10.5, meta: { callsign: "OBSERVER 1", sidc: "SNGP------*****" } },
  ];
  for (const t of demo) {
    await fetch("/api/tracks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(t),
    });
  }
  await refresh();
});

const pipBtn = document.getElementById("pip");
const popBtn = document.getElementById("pop");
fpvSelectEl.addEventListener("change", syncStreamSelection);

pipBtn.addEventListener("click", async () => {
  const src = encodeURIComponent(fpvState.selectedStream || DEFAULT_VIDEO_STREAM);
  window.open(`/video/pip?src=${src}`, "FMV_PIP", "width=420,height=280");
});

popBtn.addEventListener("click", () => {
  const src = encodeURIComponent(fpvState.selectedStream || DEFAULT_VIDEO_STREAM);
  window.open(`/video/view?src=${src}`, "_blank");
});

