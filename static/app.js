
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
  other: L.layerGroup().addTo(map),
};

const markersByUid = new Map();

// MIL-STD-2525C SIDC construction helpers
// Format: S-B-FFFFFF-SS-CC-OB  (15 chars, dashes omitted)
// Pos 1: Scheme        S = Warfighting
// Pos 2: Affiliation   F=Friendly H=Hostile N=Neutral U=Unknown
// Pos 3: Battle dim    G=Ground A=Air P=Space U=Subsurface S=Sea F=SOF
// Pos 4: Status        P=Present A=Anticipated
// Pos 5-10: Function ID
// Pos 11-15: Size/Mobility/Country/OB (use - for unused)

const AFFILIATION = { friendly: "F", enemy: "H", neutral: "N", unknown: "U" };

// Default SIDCs per layer (side letter is swapped in at runtime)
const LAYER_SIDC = {
  friendly: "S*GPUCI---*****",   // Ground unit - Infantry
  enemy:    "S*GPUCI---*****",   // Ground unit - Infantry
  fires:    "S*GPUCF---*****",   // Ground unit - Field Artillery
  air:      "S*APMFQ---*****",   // Air - UAV/Drone
  ew:       "S*GPEWM---*****",   // Ground - EW Jamming
  other:    "S*GP------*****",   // Ground - generic
};

const SYM_SIZE = 40;

function iconFor(track) {
  const aff = AFFILIATION[track.side] || "U";
  const sidc_template = track.meta?.sidc || LAYER_SIDC[track.layer] || LAYER_SIDC.other;
  // Insert affiliation at position 2
  const sidc = sidc_template[0] + aff + sidc_template.slice(2);

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
        <div class="badge badge-${t.side}">${t.side}</div>
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
    { uid: "FRD-ALPHA1", side: "friendly", layer: "friendly", lat: 50.1109, lon: 8.6821,
      meta: { callsign: "ALPHA 1", sidc: "SFGPUCI---D*****" } },       // Friendly Infantry Company
    { uid: "FRD-BRAVO2", side: "friendly", layer: "friendly", lat: 52.5200, lon: 13.4050,
      meta: { callsign: "BRAVO 2", sidc: "SFGPUCA---D*****" } },       // Friendly Armor Company
    { uid: "ENY-RED1", side: "enemy", layer: "enemy", lat: 51.0504, lon: 13.7373,
      meta: { callsign: "RED 1", sidc: "SHGPUCIM--E*****" } },         // Hostile Mechanized Infantry Battalion
    { uid: "ENY-RED2", side: "enemy", layer: "enemy", lat: 50.9300, lon: 14.1200,
      meta: { callsign: "RED 2", sidc: "SHGPUCA---D*****" } },         // Hostile Armor Company
    { uid: "FIRES-PLT", side: "friendly", layer: "fires", lat: 49.4521, lon: 11.0767,
      meta: { callsign: "STEEL RAIN", sidc: "SFGPUCFHE-D*****" } },    // Friendly Howitzer Battery
    { uid: "AIR-UAS1", side: "friendly", layer: "air", lat: 48.1351, lon: 11.5820,
      meta: { callsign: "SHADOW 6", sidc: "SFAPMFQ---*****" } },       // Friendly UAV
    { uid: "AIR-ROTARY1", side: "friendly", layer: "air", lat: 49.8700, lon: 8.9200,
      meta: { callsign: "DUSTOFF 9", sidc: "SFAPMHA---*****" } },      // Friendly Attack Helicopter
    { uid: "EW-TEAM1", side: "friendly", layer: "ew", lat: 53.5511, lon: 9.9937,
      meta: { callsign: "SPECTRE", sidc: "SFGPEWM---C*****" } },       // Friendly EW Jamming Platoon
    { uid: "NEU-OBS1", side: "neutral", layer: "other", lat: 51.5000, lon: 10.5000,
      meta: { callsign: "OBSERVER 1", sidc: "SNGP------*****" } },     // Neutral ground track
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
