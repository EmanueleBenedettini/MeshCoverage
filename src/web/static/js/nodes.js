/**
 * MeshMonitor — Pagina gestione nodi
 */
'use strict';

let editingNodeId = null;

document.addEventListener('DOMContentLoaded', () => {
  initFilters();
  initModal();
});

// ── Filtri tabella ─────────────────────────────────────────────────────────

function initFilters() {
  const searchInp   = document.getElementById('filter-nodes');
  const freqSel     = document.getElementById('filter-freq');
  const presetSel   = document.getElementById('filter-preset');
  const completeChk = document.getElementById('filter-complete-only');

  [searchInp, freqSel, presetSel, completeChk].forEach(el => {
    el?.addEventListener('input', filterTable);
    el?.addEventListener('change', filterTable);
  });
}

function filterTable() {
  const search   = document.getElementById('filter-nodes')?.value.toLowerCase() || '';
  const freq     = document.getElementById('filter-freq')?.value || '';
  const preset   = document.getElementById('filter-preset')?.value || '';
  const completeOnly = document.getElementById('filter-complete-only')?.checked;

  document.querySelectorAll('#nodes-tbody .node-row').forEach(row => {
    const matchSearch  = !search  || row.dataset.id.includes(search) || row.dataset.name.includes(search);
    const matchFreq    = !freq    || row.dataset.freq === freq;
    const matchPreset  = !preset  || row.dataset.preset === preset;
    const matchComplete = !completeOnly || row.dataset.complete === 'true';

    row.style.display = (matchSearch && matchFreq && matchPreset && matchComplete) ? '' : 'none';
  });
}

// ── Modal ──────────────────────────────────────────────────────────────────

function initModal() {
  document.getElementById('btn-add-node')?.addEventListener('click', () => {
    openModal(null);
  });
}

function openModal(nodeId) {
  editingNodeId = nodeId || null;
  const modal = document.getElementById('node-modal');
  const title = document.getElementById('modal-title');
  const form  = document.getElementById('node-form');
  if (!modal || !form) return;

  form.reset();
  if (nodeId) {
    title.textContent = 'Modifica nodo';
    // Carica dati nodo corrente
    apiGet(`/api/nodes/${nodeId}`).then(node => fillForm(form, node)).catch(console.error);
    // Blocca modifica ID
    form.querySelector('[name=id]').readOnly = true;
    form.querySelector('[name=id]').value = nodeId;
  } else {
    title.textContent = 'Aggiungi nodo';
    form.querySelector('[name=id]').readOnly = false;
  }

  modal.classList.remove('hidden');
}

function fillForm(form, node) {
  const set = (name, val) => {
    const el = form.querySelector(`[name="${name}"]`);
    if (el && val !== null && val !== undefined) el.value = val;
  };
  set('id', node.id);
  set('short_name', node.short_name);
  set('long_name', node.long_name);
  if (node.position) {
    set('lat', node.position.lat);
    set('lon', node.position.lon);
  }
  set('ground_height_m', node.ground_height_m);
  set('frequency_mhz', node.frequency_mhz);
  set('modem_preset', node.modem_preset);
  set('notes', node.notes);
  if (node.antenna) {
    set('tx_power_dbm', node.antenna.tx_power_dbm);
    set('antenna_type', node.antenna.type);
    set('gain_dbi', node.antenna.gain_dbi);
    set('azimuth_deg', node.antenna.azimuth_deg);
    set('beamwidth_deg', node.antenna.beamwidth_deg);
    set('gain_min_dbi', node.antenna.gain_min_dbi);
    set('gain_max_dbi', node.antenna.gain_max_dbi);
  }
}

function closeModal() {
  document.getElementById('node-modal')?.classList.add('hidden');
  editingNodeId = null;
}

async function saveNode() {
  const form = document.getElementById('node-form');
  if (!form) return;

  const g = name => {
    const el = form.querySelector(`[name="${name}"]`);
    return el?.value?.trim() || null;
  };
  const gf = name => { const v = g(name); return v ? parseFloat(v) : null; };
  const gi = name => { const v = g(name); return v ? parseInt(v) : null; };

  const body = {
    id: g('id'),
    short_name: g('short_name'),
    long_name: g('long_name'),
    frequency_mhz: gi('frequency_mhz'),
    modem_preset: g('modem_preset'),
    ground_height_m: gf('ground_height_m'),
    notes: g('notes'),
  };

  const lat = gf('lat'), lon = gf('lon');
  if (lat !== null && lon !== null) {
    body.position = { lat, lon };
  }

  const txPower = gf('tx_power_dbm');
  const antType = g('antenna_type');
  const gainDbi = gf('gain_dbi');
  if (txPower || antType || gainDbi) {
    body.antenna = {
      tx_power_dbm: txPower,
      type: antType,
      gain_dbi: gainDbi,
      azimuth_deg: gf('azimuth_deg'),
      beamwidth_deg: gf('beamwidth_deg'),
      gain_min_dbi: gf('gain_min_dbi'),
      gain_max_dbi: gf('gain_max_dbi'),
    };
  }

  try {
    if (editingNodeId) {
      await fetch(`/api/nodes/${editingNodeId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }).then(r => { if (!r.ok) throw new Error(r.statusText); return r.json(); });
      showToast('Nodo aggiornato', 'success');
    } else {
      await apiPost('/api/nodes', body);
      showToast('Nodo creato', 'success');
    }
    closeModal();
    setTimeout(() => location.reload(), 800);
  } catch (e) {
    showToast('Errore: ' + e.message, 'error');
  }
}

function editNode(nodeId) {
  openModal(nodeId);
}

async function deleteNode(nodeId) {
  if (!confirm(`Eliminare il nodo ${nodeId}? L'operazione non è reversibile.`)) return;
  try {
    await apiDelete(`/api/nodes/${nodeId}`);
    showToast(`Nodo ${nodeId} eliminato`, 'success');
    document.querySelector(`.node-row[data-id="${nodeId}"]`)?.remove();
  } catch (e) {
    showToast('Errore: ' + e.message, 'error');
  }
}
