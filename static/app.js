
const STALE_SEC = 90;

const statusEl = document.getElementById("status");
const tracklistEl = document.getElementById("tracklist");

const map = L.map('map', { zoomControl: true }).setView([50.1109, 8.6821], 6);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19, attribution: '&copy; OpenStreetMap'
}).addTo(map);

const layers = {
  friendly: L.layerGroup().addTo(map),
  enemy: L.layerGroup().addTo(map),
  fires: L.layerGroup().addTo(map),
  air: L.layerGroup().addTo(map),
  ew: L.layerGroup().addTo(map),
};

const markersByUid = new Map();

function iconFor(track) {
  // Simple symbology: circle for friendly, square for enemy, triangle for others.
  // (Production: MIL-STD-2525/APP-6 with proper SVG icon sets.)
  let html = "";
  let cls = "sym sym-other";
  if (track.side === "friendly") { html = "●"; cls = "sym sym-friendly"; }
  else if (track.side === "enemy") { html = "■"; cls = "sym sym-enemy"; }
  else { html = "▲"; cls = "sym sym-other"; }

  return L.divIcon({
    className: cls,
    html,
    iconSize: [20, 20],
    iconAnchor: [10, 10]
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
    // move to correct layer if changed
    Object.values(layers).forEach(g => { try { g.removeLayer(marker); } catch(e) {} });
    marker.addTo(group);
  }

  const cs = track.meta?.callsign || track.uid;
  const updated = new Date(track.updated_at).toLocaleString();
  marker.bindPopup(`<b>${cs}</b><br/>${track.side} • ${track.layer}<br/>Updated: ${updated}`);
}

function updateTrackList(tracks, serverTimeIso) {
  const now = new Date(serverTimeIso);
  tracklistEl.innerHTML = "";
  for (const t of tracks) {
    const updated = new Date(t.updated_at);
    const ageSec = Math.max(0, (now - updated) / 1000.0);
    const stale = ageSec > STALE_SEC;

    const div = document.createElement("div");
    div.className = "track" + (stale ? " stale" : "");

    const cs = t.meta?.callsign || t.uid;
    div.innerHTML = `
      <div class="row">
        <div><b>${cs}</b></div>
        <div class="badge">${t.side}</div>
      </div>
      <div class="small">${t.layer} • age ${ageSec.toFixed(0)}s</div>
      <div class="small">${t.lat.toFixed(4)}, ${t.lon.toFixed(4)}</div>
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
    statusEl.textContent = `Tracks: ${tracks.length} • Server: ${new Date(data.server_time).toLocaleTimeString()}`;
    for (const t of tracks) setMarker(t);
    updateTrackList(tracks, data.server_time);
  } catch (e) {
    statusEl.textContent = "Disconnected — showing last known positions";
  }
}

setInterval(refresh, 1500);
refresh();

// layer toggles
document.querySelectorAll('input[type="checkbox"][data-layer]').forEach(cb => {
  cb.addEventListener("change", () => {
    const name = cb.getAttribute("data-layer");
    const group = layers[name];
    if (!group) return;
    if (cb.checked) group.addTo(map);
    else map.removeLayer(group);
  });
});

// buttons
document.getElementById("btn-center").addEventListener("click", () => map.setView([50.1109, 8.6821], 6));

document.getElementById("btn-demo").addEventListener("click", async () => {
  const demo = [
    { uid: "FRD-ALPHA1", side: "friendly", layer: "friendly", lat: 50.1109, lon: 8.6821, meta: { callsign: "ALPHA 1" } },
    { uid: "FRD-BRAVO2", side: "friendly", layer: "friendly", lat: 52.5200, lon: 13.4050, meta: { callsign: "BRAVO 2" } },
    { uid: "ENY-RED1", side: "enemy", layer: "enemy", lat: 51.0504, lon: 13.7373, meta: { callsign: "RED 1" } },
    { uid: "FIRES-PLT", side: "friendly", layer: "fires", lat: 49.4521, lon: 11.0767, meta: { callsign: "Fires PLT" } },
    { uid: "AIR-UAS1", side: "friendly", layer: "air", lat: 48.1351, lon: 11.5820, meta: { callsign: "UAS 1" } },
    { uid: "EW-TEAM1", side: "friendly", layer: "ew", lat: 53.5511, lon: 9.9937, meta: { callsign: "EW TEAM 1" } },
  ];
  for (const t of demo) {
    await fetch("/api/tracks", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(t) });
  }
  await refresh();
});

// PiP
const pipBtn = document.getElementById("pip");
const popBtn = document.getElementById("pop");
const mjpegImg = document.getElementById("mjpeg");

pipBtn.addEventListener("click", async () => {
  // PiP for <img> isn't supported. We simulate by opening a small window.
  window.open("/video/pip", "FMV_PIP", "width=420,height=280");
});

popBtn.addEventListener("click", () => window.open("/video/view", "_blank"));
