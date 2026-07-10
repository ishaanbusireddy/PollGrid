/* socket.js — /ws/feed client with exponential-backoff reconnect and a
   REST-polling fallback trigger: if the socket has been down for 60s the
   onFallbackStart callback fires (App then polls /api/stories?since= every
   15s); onFallbackStop fires once the socket is healthy again. */

export class FeedSocket {
  /**
   * @param {{onMessage:(frame:object)=>void, onStatus:(up:boolean)=>void,
   *          onFallbackStart:()=>void, onFallbackStop:()=>void}} hooks
   */
  constructor(hooks) {
    this.hooks = hooks;
    this.ws = null;
    this.backoff = 1000;         // ms, doubles to a 30s cap
    this.downSince = null;
    this.fallbackActive = false;
    this.closed = false;
    this._reconnectTimer = null;
    this._fallbackTimer = null;
  }

  connect() {
    if (this.closed) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    let ws;
    try {
      ws = new WebSocket(`${proto}//${location.host}/ws/feed`);
    } catch (e) {
      this._onDown();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.backoff = 1000;
      this.downSince = null;
      if (this.fallbackActive) {
        this.fallbackActive = false;
        this.hooks.onFallbackStop && this.hooks.onFallbackStop();
      }
      clearTimeout(this._fallbackTimer);
      this.hooks.onStatus && this.hooks.onStatus(true);
    };
    ws.onmessage = (ev) => {
      let frame = null;
      try { frame = JSON.parse(ev.data); } catch (e) { return; }
      if (frame && frame.type) this.hooks.onMessage && this.hooks.onMessage(frame);
    };
    ws.onclose = () => this._onDown();
    ws.onerror = () => { try { ws.close(); } catch (e) { /* already closed */ } };
  }

  _onDown() {
    if (this.closed) return;
    this.hooks.onStatus && this.hooks.onStatus(false);
    if (!this.downSince) {
      this.downSince = Date.now();
      // fallback engages after the socket has been down 60s
      clearTimeout(this._fallbackTimer);
      this._fallbackTimer = setTimeout(() => {
        if (this.downSince && !this.fallbackActive && !this.closed) {
          this.fallbackActive = true;
          this.hooks.onFallbackStart && this.hooks.onFallbackStart();
        }
      }, 60000);
    }
    clearTimeout(this._reconnectTimer);
    this._reconnectTimer = setTimeout(() => this.connect(), this.backoff);
    this.backoff = Math.min(this.backoff * 2, 30000);
  }

  close() {
    this.closed = true;
    clearTimeout(this._reconnectTimer);
    clearTimeout(this._fallbackTimer);
    if (this.ws) { try { this.ws.close(); } catch (e) { /* noop */ } }
  }
}
