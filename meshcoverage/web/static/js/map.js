/**
 * MeshMonitor — Logica mappa principale (Leaflet.js)
 *
 * Gestisce:
 * - Layer heatmap (leaflet.heat)
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

  // Leggi valori iniziali
  state.freq    = selFreq?.value   ? parseInt(selFreq.value)   : null;
  state.preset  = selPreset?.value || null;
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

async function loadHeatmap() {
  // Rimuovi layer precedente
  if (heatLayer) { map.removeLayer(heatLayer); heatLayer = null; }
  if (!state.freq || !state.preset || !state.showHeatmap) return;

  try {
    const url = `/api/heatmaps/${state.freq}/${state.preset}?min_budget=${state.minBudget}`;
    const geojson = await apiGet(url);
    if (!geojson?.features?.length) return;

    // Converti GeoJSON in formato leaflet.heat: [[lat, lon, intensity], ...]
    const maxLB = 30;
    const points = geojson.features.map(f => {
      const [lon, lat] = f.geometry.coordinates;
      const lb = f.properties.link_budget_db || 0;
      const intensity = Math.max(0, Math.min(1, (lb + 10) / (maxLB + 10)));
      return [lat, lon, intensity];
    });

    heatLayer = L.heatLayer(points, {
      radius: 18,
      blur: 15,
      maxZoom: 17,
      gradient: {
        0.0: '#1e3a5f',
        0.3: '#1d4ed8',
        0.5: '#f59e0b',
        0.7: '#f97316',
        1.0: '#22c55e',
      },
    }).addTo(map);

  } catch (e) {
    // Heatmap non disponibile — non è un errore bloccante
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
      const lb = f.properties.min_link_budget || 0;
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
  renderNodeMarkers();  // aggiorna stile marker
  await showNodeDetail(nodeId);

  // Se il nodo ha una copertura calcolata, mostrala
  await loadNodeCoverage(nodeId);
}

async function showNodeDetail(nodeId) {
  const node = allNodes[nodeId];
  if (!node) return;

  const detail = document.getElementById('node-detail');
  if (!detail) return;
  detail.style.display = 'block';

  document.getElementById('nd-title').textContent =
    `${node.short_name || node.id}`;

  // Info grid
  const info = document.getElementById('nd-info');
  info.innerHTML = infoItem('ID', `<code>${node.id}</code>`) +
    infoItem('Nome', node.long_name || '—') +
    infoItem('Frequenza', node.frequency_mhz ? `${node.frequency_mhz} MHz` : '—') +
    infoItem('Preset', node.modem_preset || '—') +
    infoItem('Posizione', node.position ? `${node.position.lat.toFixed(4)}, ${node.position.lon.toFixed(4)}` : '—') +
    infoItem('Altezza dal suolo', node.ground_height_m ? `${node.ground_height_m} m` : '—') +
    infoItem('Hardware', node.hardware_model || '—') +
    infoItem('Ultimo contatto', node.last_seen ? new Date(node.last_seen).toLocaleString('it-IT') : '—');

  if (node.antenna) {
    info.innerHTML += infoItem('Antenna', node.antenna.type || '—') +
      infoItem('Guadagno', node.antenna.gain_dbi ? `${node.antenna.gain_dbi} dBi` : '—') +
      infoItem('TX Power', node.antenna.tx_power_dbm ? `${node.antenna.tx_power_dbm} dBm` : '—') +
      infoItem('ERP', node.erp_warning !== null ?
        `${(node.antenna.tx_power_dbm + node.antenna.gain_dbi).toFixed(1)} dBm${node.erp_warning ? ' ⚠' : ''}` : '—');
  }

  // Diagramma radiazione
  drawRadiationDiagram(node);

  // Connessioni dirette
  await loadNodeLinksDetail(nodeId);

  // Link per modifica
  document.getElementById('btn-edit-node').href = `/nodes`;
}

function infoItem(label, value) {
  return `<div class="info-item"><span class="info-label">${label}</span><span class="info-value">${value}</span></div>`;
}

function closeNodeDetail() {
  selectedNodeId = null;
  document.getElementById('node-detail').style.display = 'none';
  // Rimuovi layer copertura nodo singolo se presente
  if (window._nodeCoverageLayer) {
    map.removeLayer(window._nodeCoverageLayer);
    window._nodeCoverageLayer = null;
  }
  renderNodeMarkers();
  // Ricarica heatmap generale
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

  const ant = node.antenna;
  const isOmni = !ant || !ant.beamwidth_deg || ant.beamwidth_deg >= 360;
  const azimuth = ant?.azimuth_deg || 0;
  const beamwidth = ant?.beamwidth_deg || 360;
  const gainNorm = ant?.gain_dbi ? Math.min(1, ant.gain_dbi / 12) : 0.5;

  // Sfondo griglia
  ctx.strokeStyle = 'rgba(255,255,255,0.08)';
  ctx.lineWidth = 1;
  for (let i = 1; i <= 4; i++) {
    ctx.beginPath();
    ctx.arc(cx, cy, (r / 4) * i, 0, Math.PI * 2);
    ctx.stroke();
  }
  // Assi cardinali
  ['N','E','S','O'].forEach((label, i) => {
    const angle = (i * Math.PI / 2) - Math.PI / 2;
    const tx = cx + (r + 12) * Math.cos(angle);
    const ty = cy + (r + 12) * Math.sin(angle);
    ctx.fillStyle = 'rgba(255,255,255,0.3)';
    ctx.font = '10px Inter';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(label, tx, ty);
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + r * Math.cos(angle), cy + r * Math.sin(angle));
    ctx.strokeStyle = 'rgba(255,255,255,0.06)';
    ctx.stroke();
  });

  // Pattern di radiazione
  ctx.beginPath();
  const steps = 360;
  for (let deg = 0; deg <= steps; deg++) {
    const rad = (deg - 90) * Math.PI / 180;  // 0° = Nord
    let g;
    if (isOmni) {
      g = gainNorm;
    } else {
      const diff = Math.abs(((deg - azimuth) + 180) % 360 - 180);
      const halfBW = beamwidth / 2;
      if (diff <= halfBW) {
        g = gainNorm * (1 - 0.3 * (diff / halfBW));
      } else {
        g = 0.08;  // back lobe piccolo
      }
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

  // Punto centrale
  ctx.beginPath();
  ctx.arc(cx, cy, 3, 0, Math.PI * 2);
  ctx.fillStyle = '#818cf8';
  ctx.fill();
}

async function loadNodeLinksDetail(nodeId) {
  const list = document.getElementById('nd-links-list');
  const empty = document.getElementById('nd-links-empty');
  if (!list) return;
  list.innerHTML = '';

  try {
    const data = await apiGet(`/api/links/node/${nodeId}`);
    const links = data.links || [];

    if (!links.length) {
      empty.style.display = 'block';
      return;
    }
    empty.style.display = 'none';

    links.slice(0, 10).forEach(link => {
      const peerId = link.node_a_id === nodeId ? link.node_b_id : link.node_a_id;
      const peer = allNodes[peerId];
      const lb = link.min_link_budget;
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

// ── Copertura nodo singolo ─────────────────────────────────────────────────

async function loadNodeCoverage(nodeId) {
  // Rimuovi layer precedente
  if (window._nodeCoverageLayer) {
    map.removeLayer(window._nodeCoverageLayer);
    window._nodeCoverageLayer = null;
  }
  if (heatLayer) { map.removeLayer(heatLayer); heatLayer = null; }

  try {
    const url = `/api/coverage/${nodeId}/geojson?min_budget=${state.minBudget}`;
    const geojson = await apiGet(url);
    if (!geojson?.features?.length) return;

    const points = geojson.features.map(f => {
      const [lon, lat] = f.geometry.coordinates;
      const lb = f.properties.link_budget_db || 0;
      const intensity = Math.max(0, Math.min(1, (lb + 10) / 40));
      return [lat, lon, intensity];
    });

    window._nodeCoverageLayer = L.heatLayer(points, {
      radius: 15, blur: 12, maxZoom: 17,
      gradient: { 0: '#1e3a5f', 0.4: '#3b82f6', 0.7: '#f97316', 1: '#22c55e' },
    }).addTo(map);

  } catch (e) {
    // Copertura non calcolata — ok
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
  state.showHeatmap ? map.addLayer(heatLayer) : map.removeLayer(heatLayer);
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
