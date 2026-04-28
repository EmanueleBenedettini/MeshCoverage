/**
 * WebSocket client e utility globali per MeshMonitor.
 * Gestisce la connessione WS, i messaggi push e le notifiche toast.
 */

// ── Toast ──────────────────────────────────────────────────────────────────

function showToast(message, type = 'info', duration = 4000) {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const t = document.createElement('div');
  t.className = `toast toast-${type}`;

  const icons = {
    success: '✓', error: '✕', warning: '⚠', info: 'ℹ'
  };
  t.innerHTML = `<span class="toast-icon">${icons[type] || 'ℹ'}</span><span>${message}</span>`;
  container.appendChild(t);

  setTimeout(() => {
    t.style.opacity = '0';
    t.style.transform = 'translateX(20px)';
    t.style.transition = 'all .2s';
    setTimeout(() => t.remove(), 200);
  }, duration);
}

// ── API helper ─────────────────────────────────────────────────────────────

async function apiGet(url) {
  const r = await fetch(url);
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
}

async function apiPost(url, body = null) {
  const opts = { method: 'POST', headers: {} };
  if (body !== null) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(url, opts);
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
}

async function apiPatch(url, body) {
  const r = await fetch(url, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
}

async function apiDelete(url) {
  const r = await fetch(url, { method: 'DELETE' });
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail || r.statusText);
  }
  return r.status === 204 ? null : r.json();
}

// ── WebSocket ──────────────────────────────────────────────────────────────

class MeshWS {
  constructor() {
    this.ws = null;
    this.reconnectDelay = 3000;
    this.handlers = {};
    this._pingInterval = null;
  }

  connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}/ws`;
    this.ws = new WebSocket(url);

    this.ws.onopen = () => {
      this._setStatus(true);
      this.reconnectDelay = 3000;
      this._pingInterval = setInterval(() => this._ping(), 25000);
    };

    this.ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        this._dispatch(msg);
      } catch {}
    };

    this.ws.onclose = () => {
      this._setStatus(false);
      clearInterval(this._pingInterval);
      setTimeout(() => this.connect(), this.reconnectDelay);
      this.reconnectDelay = Math.min(this.reconnectDelay * 1.5, 30000);
    };

    this.ws.onerror = () => {
      this.ws.close();
    };
  }

  on(type, handler) {
    if (!this.handlers[type]) this.handlers[type] = [];
    this.handlers[type].push(handler);
    return this; // chainable
  }

  _dispatch(msg) {
    const handlers = this.handlers[msg.type] || [];
    handlers.forEach(h => h(msg));
    // Wildcard
    (this.handlers['*'] || []).forEach(h => h(msg));
  }

  _ping() {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: 'ping' }));
    }
  }

  _setStatus(connected) {
    const dot = document.getElementById('ws-status');
    if (!dot) return;
    dot.className = 'status-dot ' + (connected ? 'status-connected' : 'status-disconnected');
    dot.title = connected ? 'WebSocket connesso' : 'WebSocket disconnesso';
  }
}

// Singleton globale
const meshWS = new MeshWS();

// Avvia WS quando il DOM è pronto
document.addEventListener('DOMContentLoaded', () => {
  meshWS.connect();
});
