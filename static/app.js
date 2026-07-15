/** Shared helpers for lobby / room / table */
(function (global) {
  function getCookie(name) {
    const m = document.cookie
      .split(';')
      .map((c) => c.trim())
      .find((c) => c.startsWith(name + '='));
    if (!m) return '';
    try {
      return decodeURIComponent(m.split('=').slice(1).join('='));
    } catch (e) {
      return '';
    }
  }

  function currentUsername() {
    return localStorage.getItem('username') || getCookie('username') || '';
  }

  function requireLogin() {
    const u = currentUsername();
    if (!u) {
      window.location.href = '/';
      return null;
    }
    return u;
  }

  async function api(url, options = {}) {
    const res = await fetch(url, {
      credentials: 'include',
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
      ...options,
    });
    let data = null;
    try {
      data = await res.json();
    } catch (e) {
      data = { success: false, message: '响应解析失败' };
    }
    if (res.status === 401) {
      localStorage.removeItem('username');
      window.location.href = '/';
      throw new Error('unauthorized');
    }
    return { res, data };
  }

  class GameSocket {
    constructor() {
      this.ws = null;
      this.handlers = {};
      this._retry = 0;
      this._closed = false;
    }

    on(type, fn) {
      if (!this.handlers[type]) this.handlers[type] = [];
      this.handlers[type].push(fn);
    }

    _emit(type, msg) {
      (this.handlers[type] || []).forEach((fn) => {
        try {
          fn(msg);
        } catch (e) {
          console.error(e);
        }
      });
      (this.handlers['*'] || []).forEach((fn) => {
        try {
          fn(msg);
        } catch (e) {
          console.error(e);
        }
      });
    }

    connect() {
      this._closed = false;
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      this.ws = new WebSocket(`${proto}://${location.host}/ws`);
      this.ws.addEventListener('open', () => {
        this._retry = 0;
        this._emit('_open', {});
      });
      this.ws.addEventListener('message', (ev) => {
        let msg;
        try {
          msg = JSON.parse(ev.data);
        } catch (e) {
          return;
        }
        this._emit(msg.type || 'unknown', msg);
      });
      this.ws.addEventListener('close', () => {
        this._emit('_close', {});
        if (!this._closed) {
          const delay = Math.min(8000, 500 + this._retry * 500);
          this._retry += 1;
          setTimeout(() => this.connect(), delay);
        }
      });
    }

    send(type, payload = {}, roomId = null) {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
      const msg = { type, payload };
      if (roomId) msg.room_id = roomId;
      this.ws.send(JSON.stringify(msg));
      return true;
    }

    close() {
      this._closed = true;
      if (this.ws) this.ws.close();
    }
  }

  function roomIdFromPathOrQuery() {
    const parts = location.pathname.split('/').filter(Boolean);
    if (parts[0] === 'room' || parts[0] === 'table') {
      if (parts[1]) return parts[1].toUpperCase();
    }
    const q = new URLSearchParams(location.search).get('id');
    return q ? q.toUpperCase() : '';
  }

  global.TBK = { getCookie, currentUsername, requireLogin, api, GameSocket, roomIdFromPathOrQuery };
})(window);
