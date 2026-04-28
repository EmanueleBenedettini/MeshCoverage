/**
 * MeshCoverage — Settings page
 */
'use strict';

document.addEventListener('DOMContentLoaded', () => {
  loadInputStatus();
  loadDemStatus();
  initActions();
  initWsHandlers();
});

// ── Input ────────────────────────────────────────────────────────────

async function loadInputStatus() {
  const container = document.getElementById('input-status-content');
  if (!container) return;

  try {
    const data = await apiGet('/api/input/status');
    container.innerHTML = renderInputStatus(data);
  } catch (e) {
    container.innerHTML = `<p class="text-muted">Errore caricamento stato: ${e.message}</p>`;
  }
}

function renderInputStatus(data) {
  let html = '';

  // MQTT
  html += `<div class="status-section">`;
  html += `<h3>MQTT <span class="conn-indicator">`;
  if (data.mqtt.enabled) {
    const conn = data.mqtt.stats?.connected;
    html += `<span class="status-dot ${conn ? 'status-connected' : 'status-disconnected'}"></span>
             ${conn ? 'Connesso' : 'Disconnesso'}`;
  } else {
    html += `<span style="color:var(--text3)">Disabilitato</span>`;
  }
  html += `</span></h3>`;

  if (data.mqtt.enabled && data.mqtt.stats) {
    const s = data.mqtt.stats;
    html += statRows([
      ['Broker', `${s.broker}:${s.port}`],
      ['Topic', s.topic],
      ['Pacchetti ricevuti', s.packets_received],
      ['Nodi aggiornati', s.nodes_updated],
      ['Errori', s.errors],
    ]);
  }
  html += `</div>`;

  // Direct
  html += `<div class="status-section">`;
  html += `<h3>Connessione diretta <span class="conn-indicator">`;
  if (data.direct.enabled) {
    const conn = data.direct.stats?.connected;
    html += `<span class="status-dot ${conn ? 'status-connected' : 'status-disconnected'}"></span>
             ${conn ? 'Connesso' : 'Disconnesso'}`;
  } else {
    html += `<span style="color:var(--text3)">Disabilitata</span>`;
  }
  html += `</span></h3>`;

  if (data.direct.enabled && data.direct.stats) {
    const s = data.direct.stats;
    html += statRows([
      ['Host', `${s.host}:${s.port}`],
      ['Pacchetti ricevuti', s.packets_received],
      ['Nodi aggiornati', s.nodes_updated],
      ['Errori', s.errors],
    ]);
  }
  html += `</div>`;

  return html;
}

function statRows(rows) {
  return rows.map(([k, v]) =>
    `<div class="stat-row"><span class="stat-key">${k}</span><span class="stat-val">${v ?? '—'}</span></div>`
  ).join('');
}

// ── Stato DEM ──────────────────────────────────────────────────────────────

async function loadDemStatus() {
  const container = document.getElementById('dem-status-content');
  if (!container) return;

  try {
    const data = await apiGet('/api/dem/status');
    if (!data.initialized || data.file_count === 0) {
      container.innerHTML = `
        <p class="text-warning" style="margin-bottom:10px">⚠ Nessun file DEM caricato</p>
        <p class="card-note">Copiare file GeoTIFF (.tif) nella directory:<br>
        <code>${data.message?.split(': ')[1] || 'data/dem/'}</code></p>`;
      return;
    }

    const b = data.total_bounds;
    container.innerHTML = statRows([
      ['File caricati', data.file_count],
      ['Lat range', `${b.minlat.toFixed(3)}° → ${b.maxlat.toFixed(3)}°`],
      ['Lon range', `${b.minlon.toFixed(3)}° → ${b.maxlon.toFixed(3)}°`],
    ]);
  } catch (e) {
    container.innerHTML = `<p class="text-muted">Errore: ${e.message}</p>`;
  }
}

// ── Azioni ─────────────────────────────────────────────────────────────────

function initActions() {
  // Start / Stop input
  document.getElementById('btn-start-input')?.addEventListener('click', async () => {
    try {
      await apiPost('/api/input/start');
      showToast('Servizi input avviati', 'success');
      setTimeout(loadInputStatus, 1500);
    } catch (e) { showToast('Errore: ' + e.message, 'error'); }
  });

  document.getElementById('btn-stop-input')?.addEventListener('click', async () => {
    try {
      await apiPost('/api/input/stop');
      showToast('Servizi input fermati', 'info');
      setTimeout(loadInputStatus, 1500);
    } catch (e) { showToast('Errore: ' + e.message, 'error'); }
  });

  document.getElementById('btn-refresh-status')?.addEventListener('click', () => {
    loadInputStatus();
    loadDemStatus();
  });

  // Test connessione diretta
  document.getElementById('btn-test-connection')?.addEventListener('click', async () => {
    const host = document.getElementById('test-host')?.value?.trim();
    const port = parseInt(document.getElementById('test-port')?.value || '4403');
    const resultDiv = document.getElementById('test-result');

    if (!host) { showToast('Inserire un indirizzo host', 'warning'); return; }

    resultDiv.className = 'test-result';
    resultDiv.textContent = 'Test in corso...';
    resultDiv.classList.remove('hidden');

    try {
      const data = await apiPost('/api/input/test/direct', { host, port });
      resultDiv.className = `test-result ${data.reachable ? 'test-ok' : 'test-fail'}`;
      resultDiv.textContent = data.message;
    } catch (e) {
      resultDiv.className = 'test-result test-fail';
      resultDiv.textContent = 'Errore: ' + e.message;
    }
  });

  // Calcolo copertura
  document.getElementById('btn-compute-all')?.addEventListener('click', async () => {
    try {
      await apiPost('/api/coverage/compute/all');
      showToast('Calcolo avviato per tutti i nodi', 'info');
    } catch (e) { showToast('Errore: ' + e.message, 'error'); }
  });

  document.getElementById('btn-regen-heatmaps')?.addEventListener('click', async () => {
    try {
      await apiPost('/api/heatmaps/generate');
      showToast('Rigenerazione heatmap avviata', 'info');
    } catch (e) { showToast('Errore: ' + e.message, 'error'); }
  });

  document.getElementById('btn-recompute-links')?.addEventListener('click', async () => {
    try {
      await apiPost('/api/links/compute');
      showToast('Calcolo connessioni avviato', 'info');
    } catch (e) { showToast('Errore: ' + e.message, 'error'); }
  });
}

// ── WebSocket handlers ─────────────────────────────────────────────────────

function initWsHandlers() {
  const progressWrap = document.getElementById('settings-progress-wrap');
  const progressBar  = document.getElementById('settings-progress-bar');
  const progressLbl  = document.getElementById('settings-progress-label');

  meshWS
    .on('compute_started', () => {
      progressWrap?.classList.remove('hidden');
      if (progressBar) progressBar.style.width = '0%';
      if (progressLbl) progressLbl.textContent = 'Avvio...';
    })
    .on('compute_progress', msg => {
      progressWrap?.classList.remove('hidden');
      if (progressBar) progressBar.style.width = `${msg.pct || 0}%`;
      if (progressLbl) progressLbl.textContent = `${msg.pct || 0}% — ${msg.node_id || ''}`;
    })
    .on('compute_done', () => {
      if (progressBar) progressBar.style.width = '100%';
      if (progressLbl) progressLbl.textContent = 'Completato!';
      showToast('Calcolo completato con successo', 'success');
      setTimeout(() => progressWrap?.classList.add('hidden'), 4000);
    })
    .on('compute_error', msg => {
      progressWrap?.classList.add('hidden');
      showToast('Errore: ' + msg.error, 'error');
    });
}
