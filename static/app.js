const STALE_SEC = 90;

const statusEl = document.getElementById("status");
const tracklistEl = document.getElementById("tracklist");

const map = L.map("map", { zoomControl: true }).setView([50.1109, 8.6821], 6);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap",
}).addTo(map);

const layers = {
  friendly: L.layerGroup().addTo(map),
  enemy: L.layerGroup().addTo(map),
  fires: L.layerGroup().addTo(map),
  air: L.layerGroup().addTo(map),
  ew: L.layerGroup().addTo(map),
  other: L.layerGroup().addTo(map),
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

function iconFor(track) {
  const aff = AFFILIATION[track.side] || "U";
  const sidcTemplate = track.meta?.sidc || LAYER_SIDC[track.layer] || LAYER_SIDC.other;
  const sidc = sidcTemplate[0] + aff + sidcTemplate.slice(2);

  const sym = new ms.Symbol(sidc, { size: SYM_SIZE });
  const anchor = sym.getAnchor();

  return L.divIcon({
    className: "",
    html: sym.asSVG(),
    iconSize: [sym.getSize().width, sym.getSize().height],
    iconAnchor: [anchor.x, anchor.y],
  });
}

function setMarker(track) {
  const latlng = [track.lat, track.lon];
  const layerName = track.layer || "other";
  const group = layers[layerName] || layers.friendly;

  let marker = markersByUid.get(track.uid);
  if (!marker) {
    marker = L.marker(latlng, { icon: iconFor(track) });
    marker.addTo(group);
    markersByUid.set(track.uid, marker);
  } else {
    marker.setLatLng(latlng);
    marker.setIcon(iconFor(track));
    Object.values(layers).forEach((g) => {
      try {
        g.removeLayer(marker);
      } catch (e) {
        // Leaflet layer was not in this group.
      }
    });
    marker.addTo(group);
  }

  const cs = track.meta?.callsign || track.uid;
  const updated = new Date(track.updated_at).toLocaleString();
  marker.bindPopup(`<b>${cs}</b><br/>${track.side} | ${track.layer}<br/>Updated: ${updated}`);
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
    div.addEventListener("click", () => map.setView([t.lat, t.lon], Math.max(map.getZoom(), 12)));
    tracklistEl.appendChild(div);
  }
}

async function refresh() {
  try {
    const res = await fetch("/api/tracks");
    const data = await res.json();
    const tracks = data.tracks || [];
    statusEl.textContent = `Tracks: ${tracks.length} | Server: ${new Date(data.server_time).toLocaleTimeString()}`;
    for (const t of tracks) setMarker(t);
    updateTrackList(tracks, data.server_time);
  } catch (e) {
    statusEl.textContent = "Disconnected - showing last known positions";
  }
}

setInterval(refresh, 1500);
refresh();

document.querySelectorAll('input[type="checkbox"][data-layer]').forEach((cb) => {
  cb.addEventListener("change", () => {
    const name = cb.getAttribute("data-layer");
    const group = layers[name];
    if (!group) return;
    if (cb.checked) group.addTo(map);
    else map.removeLayer(group);
  });
});

document.getElementById("btn-center").addEventListener("click", () => map.setView([50.1109, 8.6821], 6));

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

pipBtn.addEventListener("click", async () => {
  window.open("/video/pip", "FMV_PIP", "width=420,height=280");
});

popBtn.addEventListener("click", () => window.open("/video/view", "_blank"));
