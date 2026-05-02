/**
 * MeshMonitor — Logica mappa principale (Leaflet.js)
 *
 * Gestisce:
 * - Layer heatmap (PixelCoverageLayer — canvas raster a pixel)
 * - Layer connessioni inter-nodo (LineString)
 * - Layer marker nodi
 * - Sidebar con dettaglio nodo e diagramma di radiazione
 * - Calcolo e refresh dati
 */

'use strict';

// ── Stato globale ──────────────────────────────────────────────────────────
let map, heatLayer, linksLayer, nodesLayer;
let allNodes = {};
let selectedNodeId = null;

const state = {
  freq: null,
  preset: null,
  minBudget: 0,
  showHeatmap: true,
  showLinks: true,
  showNodes: true,
};

// ── PixelCoverageLayer — canvas raster a pixel ─────────────────────────────
//
// Sostituisce leaflet.heat con un layer canvas che disegna rettangoli solidi
// per ogni cella della griglia. Nessuna sfocatura, nessun punto distinto
// al zoom alto: si vedono quadrati colorati che coprono la cella di griglia.

const PixelCoverageLayer = L.Layer.extend({

  initialize: function (points, options) {
    // points: [{lat, lon, lb}]
    this._points = points || [];
    L.setOptions(this, L.extend({
      gridDeg: 0.001,   // risoluzione griglia in gradi (~100 m a lat media EU)
      opacity: 0.75,
    }, options));
  },

  onAdd: function (map) {
    this._map = map;
    this._canvas = L.DomUtil.create('canvas', 'leaflet-layer');
    // 'leaflet-layer' already sets position:absolute; left:0; top:0 via Leaflet CSS.
    // pointer-events:none prevents the canvas from swallowing map clicks.
    this._canvas.style.pointerEvents = 'none';
    map.getPanes().overlayPane.appendChild(this._canvas);

    // viewreset fires when Leaflet resets the internal pixel-origin to prevent
    // floating-point drift at large pan offsets — must redraw when it fires.
    map.on('viewreset moveend zoomend resize', this._reset, this);

    // NOTE: we do NOT listen to 'move' (pan animation frames).
    // Repositioning the canvas every frame while leaving stale content causes
    // the coverage layer to appear frozen on the screen while the tiles scroll
    // underneath it.  Leaflet moves the entire overlayPane during pan, so the
    // canvas drifts with the tiles (good enough visually); on 'moveend' we
    // reposition and redraw cleanly.

    this._reset();
    return this;
  },

  onRemove: function (map) {
    if (this._canvas) {
      L.DomUtil.remove(this._canvas);
      this._canvas = null;
    }
    map.off('viewreset moveend zoomend resize', this._reset, this);
  },

  // Reposition + resize the canvas and repaint all visible points.
  _reset: function () {
    if (!this._canvas || !this._map) return;

    var size = this._map.getSize();
    this._canvas.width  = size.x;
    this._canvas.height = size.y;

    // Align the canvas top-left corner to the map container's top-left corner.
    // containerPointToLayerPoint([0,0]) returns the offset needed to counteract
    // the overlayPane's own CSS transform, keeping the canvas at container (0,0).
    var topLeft = this._map.containerPointToLayerPoint([0, 0]);
    L.DomUtil.setPosition(this._canvas, topLeft);

    this._draw();
  },

  _draw: function () {
    if (!this._canvas || !this._map || !this._points.length) return;

    var map    = this._map;
    var canvas = this._canvas;
    var ctx    = canvas.getContext('2d');

    // Use canvas own dimensions, not a second call to map.getSize()
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    // Calcola quanti pixel occupa una cella di griglia al livello di zoom corrente.
    // Usiamo il centro della mappa come punto di riferimento per una stima accurata.
    var GRID   = this.options.gridDeg;
    var center = map.getCenter();
    var p0 = map.latLngToContainerPoint(L.latLng(center.lat,        center.lng));
    var p1 = map.latLngToContainerPoint(L.latLng(center.lat + GRID, center.lng + GRID));
    // +1 per evitare gap tra celle adiacenti; minimo 2px per visibilità
    var cellW = Math.max(2, Math.ceil(Math.abs(p1.x - p0.x)) + 1);
    var cellH = Math.max(2, Math.ceil(Math.abs(p1.y - p0.y)) + 1);

    // Aggiungi un margine per non perdere punti parzialmente visibili al bordo
    var bounds = map.getBounds().pad(0.05);

    var pts = this._points;
    for (var i = 0, len = pts.length; i < len; i++) {
      var pt = pts[i];

      // Filtra velocemente i punti fuori dalla vista
      if (pt.lat < bounds.getSouth() || pt.lat > bounds.getNorth() ||
          pt.lon < bounds.getWest()  || pt.lon > bounds.getEast()) continue;

      var cp = map.latLngToContainerPoint(L.latLng(pt.lat, pt.lon));
      ctx.fillStyle = lbToColor(pt.lb, this.options.opacity);
      ctx.fillRect(
        Math.round(cp.x - cellW / 2),
        Math.round(cp.y - cellH / 2),
        cellW,
        cellH
      );
    }
  },
});

// ── Gradiente colore link budget ───────────────────────────────────────────
//
// Stessa rampa colore usata in precedenza da leaflet.heat, ma ora applicata
// come colore solido di ciascun pixel (senza sfocatura).
//
//  lb ≤ -10  →  blu scuro  #1e3a5f
//  lb ≈  0   →  blu        #1d4ed8
//  lb ≈  10  →  ambra      #f59e0b
//  lb ≈  20  →  arancione  #f97316
//  lb ≥  30  →  verde      #22c55e

function lbToColor(lb, alpha) {
  var maxLB = 30;
  var t = Math.max(0, Math.min(1, (lb + 10) / (maxLB + 10)));

  // [soglia_t, r, g, b]
  var stops = [
    [0.00,  30,  58,  95],
    [0.30,  29,  78, 216],
    [0.50, 245, 158,  11],
    [0.70, 249, 115,  22],
    [1.00,  34, 197,  94],
  ];

  var i = 0;
  while (i < stops.length - 2 && stops[i + 1][0] <= t) i++;

  var t0 = stops[i][0], t1 = stops[i + 1][0];
  var f  = (t1 > t0) ? (t - t0) / (t1 - t0) : 0;

  var r = Math.round(stops[i][1] + f * (stops[i + 1][1] - stops[i][1]));
  var g = Math.round(stops[i][2] + f * (stops[i + 1][2] - stops[i][2]));
  var b = Math.round(stops[i][3] + f * (stops[i + 1][3] - stops[i][3]));

  return 'rgba(' + r + ',' + g + ',' + b + ',' + (alpha || 0.72) + ')';
}

// ── Init mappa ─────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  initMap();
  initControls();
  initWsHandlers();
  loadInitialData();
});

function initMap() {
  map = L.map('map', {
    center: [45.5, 10.0],
    zoom: 9,
    zoomControl: true,
    attributionControl: true,
  });

  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© <a href="https://openstreetmap.org">OpenStreetMap</a>',
    maxZoom: 18,
  }).addTo(map);

  linksLayer = L.layerGroup().addTo(map);
  nodesLayer = L.layerGroup().addTo(map);
}

function initControls() {
  const selFreq    = document.getElementById('sel-freq');
  const selPreset  = document.getElementById('sel-preset');
  const inpBudget  = document.getElementById('inp-min-budget');
  const chkHeatmap = document.getElementById('chk-heatmap');
  const chkLinks   = document.getElementById('chk-links');
  const chkNodes   = document.getElementById('chk-nodes');
  const btnApply   = document.getElementById('btn-apply-filters');
  const btnCompute = document.getElementById('btn-compute-all');
  const btnClose   = document.getElementById('btn-close-detail');
  const btnComputeNode = document.getElementById('btn-compute-node');

  state.freq      = selFreq?.value   ? parseInt(selFreq.value)   : null;
  state.preset    = selPreset?.value || null;
  state.minBudget = parseFloat(inpBudget?.value || '0');

  btnApply?.addEventListener('click', applyFilters);
  btnCompute?.addEventListener('click', computeAll);
  btnClose?.addEventListener('click', closeNodeDetail);

  btnComputeNode?.addEventListener('click', () => {
    if (selectedNodeId) computeNode(selectedNodeId);
  });

  chkHeatmap?.addEventListener('change', e => {
    state.showHeatmap = e.target.checked;
    toggleHeatmap();
  });
  chkLinks?.addEventListener('change', e => {
    state.showLinks = e.target.checked;
    toggleLinks();
  });
  chkNodes?.addEventListener('change', e => {
    state.showNodes = e.target.checked;
    toggleNodes();
  });
}

function initWsHandlers() {
  meshWS
    .on('compute_started', msg => {
      setComputeProgress(0, 'Calcolo avviato...');
      showProgress(true);
    })
    .on('compute_progress', msg => {
      setComputeProgress(msg.pct || 0, `${msg.pct || 0}% — ${msg.node_id || ''}`);
    })
    .on('compute_done', msg => {
      setComputeProgress(100, 'Completato!');
      setTimeout(() => showProgress(false), 3000);
      showToast('Calcolo completato', 'success');
      loadInitialData();
    })
    .on('compute_error', msg => {
      showProgress(false);
      showToast(`Errore calcolo: ${msg.error}`, 'error');
    })
    .on('node_updated', () => {
      loadNodes();
    });
}

// ── Caricamento dati ───────────────────────────────────────────────────────

async function loadInitialData() {
  await loadNodes();
  applyFilters();
}

async function loadNodes() {
  try {
    const nodes = await apiGet('/api/nodes');
    allNodes = {};
    nodes.forEach(n => allNodes[n.id] = n);
    renderNodeMarkers();
    updateNodeCountBadge();
  } catch (e) {
    showToast('Errore caricamento nodi: ' + e.message, 'error');
  }
}

function updateNodeCountBadge() {
  const badge = document.getElementById('node-count-badge');
  if (badge) badge.textContent = `${Object.keys(allNodes).length} nodi`;
}

async function applyFilters() {
  const selFreq   = document.getElementById('sel-freq');
  const selPreset = document.getElementById('sel-preset');
  const inpBudget = document.getElementById('inp-min-budget');

  state.freq      = selFreq?.value   ? parseInt(selFreq.value)   : null;
  state.preset    = selPreset?.value || null;
  state.minBudget = parseFloat(inpBudget?.value || '0');

  await Promise.all([
    loadHeatmap(),
    loadLinks(),
  ]);
}

// ── Heatmap (pixel layer) ──────────────────────────────────────────────────

async function loadHeatmap() {
  // Rimuovi layer precedente
  if (heatLayer) {
    map.removeLayer(heatLayer);
    heatLayer = null;
  }
  if (!state.freq || !state.preset || !state.showHeatmap) return;

  try {
    const url = `/api/heatmaps/${state.freq}/${state.preset}?min_budget=${state.minBudget}`;
    const geojson = await apiGet(url);
    if (!geojson?.features?.length) return;

    // Converte le feature GeoJSON in punti {lat, lon, lb}
    const points = geojson.features.map(f => ({
      lon: f.geometry.coordinates[0],
      lat: f.geometry.coordinates[1],
      lb:  f.properties.link_budget_db || 0,
    }));

    heatLayer = new PixelCoverageLayer(points);

    if (state.showHeatmap) {
      map.addLayer(heatLayer);
    }

  } catch (e) {
    // Heatmap non disponibile — non bloccante
    console.debug('Heatmap non disponibile:', e.message);
  }
}

async function loadLinks() {
  linksLayer.clearLayers();
  if (!state.freq || !state.preset || !state.showLinks) return;

  try {
    const geojson = await apiGet(`/api/links/${state.freq}/${state.preset}`);
    if (!geojson?.features?.length) return;

    geojson.features.forEach(f => {
      const lb    = f.properties.min_link_budget || 0;
      const color = lb >= 15 ? '#22c55e' : lb >= 5 ? '#f59e0b' : '#ef4444';

      const line = L.geoJSON(f, {
        style: {
          color,
          weight: 2,
          opacity: 0.75,
          dashArray: f.properties.fresnel_ok ? null : '5,4',
        },
      });

      line.bindTooltip(`
        <b>${f.properties.node_a_name}</b> ↔ <b>${f.properties.node_b_name}</b><br>
        Distanza: ${f.properties.distance_km} km<br>
        Link budget: ${lb?.toFixed(1)} dB
        ${f.properties.fresnel_ok ? '' : '<br><i>Fresnel ostruita</i>'}
      `, { sticky: true });

      linksLayer.addLayer(line);
    });

  } catch (e) {
    console.debug('Links non disponibili:', e.message);
  }
}

// ── Marker nodi ────────────────────────────────────────────────────────────

function renderNodeMarkers() {
  nodesLayer.clearLayers();
  if (!state.showNodes) return;

  Object.values(allNodes).forEach(node => {
    if (!node.position) return;

    const icon = L.divIcon({
      className: '',
      html: `<div class="node-marker ${!node.is_complete ? 'incomplete' : ''} ${selectedNodeId === node.id ? 'selected' : ''}"></div>`,
      iconSize: [14, 14],
      iconAnchor: [7, 7],
    });

    const marker = L.marker([node.position.lat, node.position.lon], { icon });

    marker.bindPopup(`
      <div>
        <b>${node.short_name || node.id}</b><br>
        <small style="color:#94a3b8">${node.id}</small><br>
        ${node.frequency_mhz ? `${node.frequency_mhz} MHz` : '—'} /
        ${node.modem_preset || '—'}<br>
        ${node.is_complete ? '✓ Completo' : '⚠ Incompleto'}
      </div>
    `);

    marker.on('click', () => selectNode(node.id));
    nodesLayer.addLayer(marker);
  });
}

// ── Selezione nodo ─────────────────────────────────────────────────────────

async function selectNode(nodeId) {
  selectedNodeId = nodeId;
  renderNodeMarkers();
  await showNodeDetail(nodeId);
  await loadNodeCoverage(nodeId);
}

async function showNodeDetail(nodeId) {
  const node = allNodes[nodeId];
  if (!node) return;

  const detail = document.getElementById('node-detail');
  if (!detail) return;
  detail.style.display = 'block';

  document.getElementById('nd-title').textContent = `${node.short_name || node.id}`;

  const info = document.getElementById('nd-info');
  info.innerHTML =
    infoItem('ID',            `<code>${node.id}</code>`) +
    infoItem('Nome',           node.long_name || '—') +
    infoItem('Frequenza',      node.frequency_mhz ? `${node.frequency_mhz} MHz` : '—') +
    infoItem('Preset',         node.modem_preset || '—') +
    infoItem('Posizione',      node.position ? `${node.position.lat.toFixed(4)}, ${node.position.lon.toFixed(4)}` : '—') +
    infoItem('Altezza dal suolo', node.ground_height_m ? `${node.ground_height_m} m` : '—') +
    infoItem('Hardware',       node.hardware_model || '—') +
    infoItem('Ultimo contatto', node.last_seen ? new Date(node.last_seen).toLocaleString('it-IT') : '—');

  if (node.antenna) {
    info.innerHTML +=
      infoItem('Antenna', node.antenna.type || '—') +
      infoItem('Guadagno', node.antenna.gain_dbi ? `${node.antenna.gain_dbi} dBi` : '—') +
      infoItem('TX Power', node.antenna.tx_power_dbm ? `${node.antenna.tx_power_dbm} dBm` : '—') +
      infoItem('ERP', node.erp_warning !== null ?
        `${(node.antenna.tx_power_dbm + node.antenna.gain_dbi).toFixed(1)} dBm${node.erp_warning ? ' ⚠' : ''}` : '—');
  }

  drawRadiationDiagram(node);
  await loadNodeLinksDetail(nodeId);
  document.getElementById('btn-edit-node').href = `/nodes`;
}

function infoItem(label, value) {
  return `<div class="info-item"><span class="info-label">${label}</span><span class="info-value">${value}</span></div>`;
}

function closeNodeDetail() {
  selectedNodeId = null;
  document.getElementById('node-detail').style.display = 'none';

  if (window._nodeCoverageLayer) {
    map.removeLayer(window._nodeCoverageLayer);
    window._nodeCoverageLayer = null;
  }

  renderNodeMarkers();
  loadHeatmap();
}

// ── Diagramma di radiazione ────────────────────────────────────────────────

function drawRadiationDiagram(node) {
  const canvas = document.getElementById('radiation-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const cx = W / 2, cy = H / 2, r = (W / 2) - 16;
  ctx.clearRect(0, 0, W, H);

  const ant       = node.antenna;
  const isOmni    = !ant || !ant.beamwidth_deg || ant.beamwidth_deg >= 360;
  const azimuth   = ant?.azimuth_deg   || 0;
  const beamwidth = ant?.beamwidth_deg || 360;
  const gainNorm  = ant?.gain_dbi ? Math.min(1, ant.gain_dbi / 12) : 0.5;

  // Griglia
  ctx.strokeStyle = 'rgba(255,255,255,0.08)';
  ctx.lineWidth = 1;
  for (let i = 1; i <= 4; i++) {
    ctx.beginPath();
    ctx.arc(cx, cy, (r / 4) * i, 0, Math.PI * 2);
    ctx.stroke();
  }
  ['N','E','S','O'].forEach((label, i) => {
    const angle = (i * Math.PI / 2) - Math.PI / 2;
    const tx = cx + (r + 12) * Math.cos(angle);
    const ty = cy + (r + 12) * Math.sin(angle);
    ctx.fillStyle = 'rgba(255,255,255,0.3)';
    ctx.font = '10px Inter';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(label, tx, ty);
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + r * Math.cos(angle), cy + r * Math.sin(angle));
    ctx.strokeStyle = 'rgba(255,255,255,0.06)';
    ctx.stroke();
  });

  // Pattern di radiazione
  ctx.beginPath();
  for (let deg = 0; deg <= 360; deg++) {
    const rad = (deg - 90) * Math.PI / 180;
    let g;
    if (isOmni) {
      g = gainNorm;
    } else {
      const diff   = Math.abs(((deg - azimuth) + 180) % 360 - 180);
      const halfBW = beamwidth / 2;
      g = diff <= halfBW ? gainNorm * (1 - 0.3 * (diff / halfBW)) : 0.08;
    }
    const px = cx + r * g * Math.cos(rad);
    const py = cy + r * g * Math.sin(rad);
    deg === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
  }
  ctx.closePath();
  const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, r);
  grad.addColorStop(0, 'rgba(99,102,241,0.6)');
  grad.addColorStop(1, 'rgba(99,102,241,0.15)');
  ctx.fillStyle = grad;
  ctx.fill();
  ctx.strokeStyle = '#6366f1';
  ctx.lineWidth = 1.5;
  ctx.stroke();

  ctx.beginPath();
  ctx.arc(cx, cy, 3, 0, Math.PI * 2);
  ctx.fillStyle = '#818cf8';
  ctx.fill();
}

async function loadNodeLinksDetail(nodeId) {
  const list  = document.getElementById('nd-links-list');
  const empty = document.getElementById('nd-links-empty');
  if (!list) return;
  list.innerHTML = '';

  try {
    const data  = await apiGet(`/api/links/node/${nodeId}`);
    const links = data.links || [];

    if (!links.length) {
      empty.style.display = 'block';
      return;
    }
    empty.style.display = 'none';

    links.slice(0, 10).forEach(link => {
      const peerId = link.node_a_id === nodeId ? link.node_b_id : link.node_a_id;
      const peer   = allNodes[peerId];
      const lb     = link.min_link_budget;
      const lbClass = lb >= 15 ? 'lb-good' : lb >= 5 ? 'lb-medium' : 'lb-poor';

      const li = document.createElement('li');
      li.className = 'link-item';
      li.innerHTML = `
        <div>
          <div class="link-name">${peer?.short_name || peerId}</div>
          <div style="font-size:11px;color:var(--text3)">${link.distance_km} km</div>
        </div>
        <span class="link-budget ${lbClass}">${lb?.toFixed(1)} dB</span>
      `;
      li.style.cursor = 'pointer';
      li.addEventListener('click', () => {
        if (peer?.position) map.setView([peer.position.lat, peer.position.lon], 12);
      });
      list.appendChild(li);
    });

  } catch (e) {
    empty.style.display = 'block';
  }
}

// ── Copertura nodo singolo (pixel layer) ───────────────────────────────────

async function loadNodeCoverage(nodeId) {
  if (window._nodeCoverageLayer) {
    map.removeLayer(window._nodeCoverageLayer);
    window._nodeCoverageLayer = null;
  }
  // Rimuovi heatmap generale mentre è attiva la copertura del nodo
  if (heatLayer) {
    map.removeLayer(heatLayer);
  }

  try {
    const url    = `/api/coverage/${nodeId}/geojson?min_budget=${state.minBudget}`;
    const geojson = await apiGet(url);
    if (!geojson?.features?.length) return;

    const points = geojson.features.map(f => ({
      lon: f.geometry.coordinates[0],
      lat: f.geometry.coordinates[1],
      lb:  f.properties.link_budget_db || 0,
    }));

    window._nodeCoverageLayer = new PixelCoverageLayer(points);
    map.addLayer(window._nodeCoverageLayer);

  } catch (e) {
    // Copertura non calcolata — ok
    console.debug('Copertura nodo non disponibile:', e.message);
  }
}

// ── Calcolo ────────────────────────────────────────────────────────────────

async function computeAll() {
  try {
    await apiPost('/api/coverage/compute/all');
    showToast('Calcolo avviato per tutti i nodi', 'info');
  } catch (e) {
    showToast('Errore: ' + e.message, 'error');
  }
}

async function computeNode(nodeId) {
  try {
    await apiPost(`/api/coverage/compute/${nodeId}?force=true`);
    showToast(`Calcolo avviato per ${nodeId}`, 'info');
  } catch (e) {
    showToast('Errore: ' + e.message, 'error');
  }
}

// ── Toggle layer ───────────────────────────────────────────────────────────

function toggleHeatmap() {
  if (!heatLayer) return;
  if (state.showHeatmap) {
    map.addLayer(heatLayer);
  } else {
    map.removeLayer(heatLayer);
  }
}

function toggleLinks() {
  state.showLinks ? map.addLayer(linksLayer) : map.removeLayer(linksLayer);
}

function toggleNodes() {
  state.showNodes ? map.addLayer(nodesLayer) : map.removeLayer(nodesLayer);
}

// ── Progress ───────────────────────────────────────────────────────────────

function showProgress(show) {
  const wrap = document.getElementById('compute-progress-wrap');
  if (wrap) wrap.classList.toggle('hidden', !show);
}

function setComputeProgress(pct, label) {
  const bar = document.getElementById('compute-progress-bar');
  const lbl = document.getElementById('compute-progress-label');
  if (bar) bar.style.width = `${pct}%`;
  if (lbl) lbl.textContent = label || `${pct}%`;
  showProgress(true);
}
