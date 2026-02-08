
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
    // --- Friendly Maneuver (ground) ---
    { uid: "FRD-ALPHA1", side: "friendly", layer: "friendly", lat: 50.1109, lon: 8.6821,
      meta: { callsign: "ALPHA 1", sidc: "SFGPUCI---D*****" } },       // Infantry Company
    { uid: "FRD-BRAVO2", side: "friendly", layer: "friendly", lat: 52.5200, lon: 13.4050,
      meta: { callsign: "BRAVO 2", sidc: "SFGPUCA---D*****" } },       // Armor Company
    { uid: "FRD-CHARLIE3", side: "friendly", layer: "friendly", lat: 50.9400, lon: 6.9578,
      meta: { callsign: "CHARLIE 3", sidc: "SFGPUCIM--D*****" } },     // Mech Infantry Company
    { uid: "FRD-DELTA4", side: "friendly", layer: "friendly", lat: 49.0069, lon: 8.4037,
      meta: { callsign: "DELTA 4", sidc: "SFGPUCRVA-D*****" } },       // Recon Company (Armored)
    { uid: "FRD-ECHO5", side: "friendly", layer: "friendly", lat: 51.2277, lon: 6.7735,
      meta: { callsign: "ECHO 5", sidc: "SFGPUCI---C*****" } },        // Infantry Platoon
    { uid: "FRD-FOXTRT6", side: "friendly", layer: "friendly", lat: 50.3569, lon: 7.5890,
      meta: { callsign: "FOXTROT 6", sidc: "SFGPUCE---D*****" } },     // Engineer Company
    { uid: "FRD-HQ-BDE", side: "friendly", layer: "friendly", lat: 50.5861, lon: 8.6743,
      meta: { callsign: "WARHORSE 6", sidc: "SFGPUH----F*****" } },    // Brigade HQ
    { uid: "FRD-BN-HQ1", side: "friendly", layer: "friendly", lat: 50.7753, lon: 9.1802,
      meta: { callsign: "IRON 6", sidc: "SFGPUH----E*****" } },        // Battalion HQ
    { uid: "FRD-SUPPLY1", side: "friendly", layer: "friendly", lat: 49.8728, lon: 8.6512,
      meta: { callsign: "BLACKHORSE LOG", sidc: "SFGPUSS---D*****" } }, // Supply Company
    { uid: "FRD-MED1", side: "friendly", layer: "friendly", lat: 49.7913, lon: 9.9356,
      meta: { callsign: "MEDEVAC 1", sidc: "SFGPUSM---C*****" } },     // Medical Platoon
    { uid: "FRD-MP1", side: "friendly", layer: "friendly", lat: 50.0782, lon: 8.2398,
      meta: { callsign: "GUARDIAN 1", sidc: "SFGPUSL---C*****" } },     // Military Police Platoon
    { uid: "FRD-SIG1", side: "friendly", layer: "friendly", lat: 50.4119, lon: 9.4078,
      meta: { callsign: "SIGNAL 6", sidc: "SFGPUUS---D*****" } },      // Signal Company
    // --- Friendly Fires ---
    { uid: "FIRES-BTRY1", side: "friendly", layer: "fires", lat: 49.4521, lon: 11.0767,
      meta: { callsign: "STEEL RAIN", sidc: "SFGPUCFHE-D*****" } },    // Howitzer Battery
    { uid: "FIRES-BTRY2", side: "friendly", layer: "fires", lat: 50.6821, lon: 10.2311,
      meta: { callsign: "THUNDER", sidc: "SFGPUCFHE-D*****" } },       // Howitzer Battery
    { uid: "FIRES-MLRS1", side: "friendly", layer: "fires", lat: 49.9137, lon: 10.8865,
      meta: { callsign: "KING OF BATTLE", sidc: "SFGPUCFR--D*****" } },// MLRS Battery
    { uid: "FIRES-MTR1", side: "friendly", layer: "fires", lat: 50.2644, lon: 11.3941,
      meta: { callsign: "HELLFIRE", sidc: "SFGPUCFM--C*****" } },      // Mortar Platoon
    // --- Friendly Air ---
    { uid: "AIR-UAS1", side: "friendly", layer: "air", lat: 48.1351, lon: 11.5820,
      meta: { callsign: "SHADOW 6", sidc: "SFAPMFQ---*****" } },       // UAV
    { uid: "AIR-UAS2", side: "friendly", layer: "air", lat: 51.4556, lon: 7.0116,
      meta: { callsign: "RAVEN 3", sidc: "SFAPMFQ---*****" } },        // UAV
    { uid: "AIR-ROTARY1", side: "friendly", layer: "air", lat: 49.8700, lon: 8.9200,
      meta: { callsign: "DUSTOFF 9", sidc: "SFAPMHA---*****" } },      // Attack Helicopter
    { uid: "AIR-ROTARY2", side: "friendly", layer: "air", lat: 50.8667, lon: 7.1431,
      meta: { callsign: "REAPER 2", sidc: "SFAPMHA---*****" } },       // Attack Helicopter
    { uid: "AIR-MEDEVAC1", side: "friendly", layer: "air", lat: 49.4875, lon: 8.4660,
      meta: { callsign: "DUSTOFF 1", sidc: "SFAPMHU---*****" } },      // Utility Helicopter
    // --- Friendly EW ---
    { uid: "EW-TEAM1", side: "friendly", layer: "ew", lat: 53.5511, lon: 9.9937,
      meta: { callsign: "SPECTRE", sidc: "SFGPEWM---C*****" } },       // EW Jamming Platoon
    { uid: "EW-TEAM2", side: "friendly", layer: "ew", lat: 51.9607, lon: 7.6261,
      meta: { callsign: "PHANTOM", sidc: "SFGPEWD---C*****" } },       // EW Direction Finding
    // --- Enemy ---
    { uid: "ENY-RED1", side: "enemy", layer: "enemy", lat: 51.0504, lon: 13.7373,
      meta: { callsign: "RED 1", sidc: "SHGPUCIM--E*****" } },         // Mech Infantry Battalion
    { uid: "ENY-RED2", side: "enemy", layer: "enemy", lat: 50.9300, lon: 14.1200,
      meta: { callsign: "RED 2", sidc: "SHGPUCA---D*****" } },         // Armor Company
    { uid: "ENY-RED3", side: "enemy", layer: "enemy", lat: 51.3397, lon: 12.3731,
      meta: { callsign: "RED 3", sidc: "SHGPUCI---E*****" } },         // Infantry Battalion
    { uid: "ENY-RED-ARTY", side: "enemy", layer: "enemy", lat: 51.1657, lon: 14.9710,
      meta: { callsign: "RED ARTY", sidc: "SHGPUCFHE-E*****" } },      // Artillery Battalion
    // --- Neutral ---
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
