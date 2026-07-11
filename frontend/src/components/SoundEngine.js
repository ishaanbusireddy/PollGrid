/* SoundEngine.js — pure Web Audio synthesis, no samples.
   - Chimes: soft two-note (poll movement), short arpeggio (breaking story),
     three-note fanfare (race call) — all ADSR-enveloped.
   - Convolution reverb built from a synthesized exponential-decay impulse.
   - Master mute persisted in localStorage; AudioContext resumed only on the
     first user gesture (autoplay policy).
   (The old volatility-tracking ambient pad was removed with the volatility
   feature — the engine is chimes-only now.) */

const KEY = 'pollgrid.sound';

export class SoundEngine {
  constructor() {
    this.ctx = null;
    this.enabled = this._loadEnabled();
    this._gestureBound = false;
  }

  _loadEnabled() {
    try { return localStorage.getItem(KEY) !== 'off'; } catch (e) { return true; }
  }

  /** Call once from App — arms the first-gesture resume. */
  armGesture() {
    if (this._gestureBound) return;
    this._gestureBound = true;
    const onGesture = () => {
      document.removeEventListener('pointerdown', onGesture);
      document.removeEventListener('keydown', onGesture);
      this._ensureContext();
    };
    document.addEventListener('pointerdown', onGesture);
    document.addEventListener('keydown', onGesture);
  }

  _ensureContext() {
    if (!this.ctx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      this.ctx = new AC();
      this._buildGraph();
    }
    if (this.ctx.state === 'suspended') this.ctx.resume();
  }

  _buildGraph() {
    const ctx = this.ctx;
    this.master = ctx.createGain();
    this.master.gain.value = this.enabled ? 0.5 : 0;
    // synthesized exponential-decay impulse → convolution reverb
    const dur = 2.2, rate = ctx.sampleRate;
    const impulse = ctx.createBuffer(2, dur * rate, rate);
    for (let ch = 0; ch < 2; ch++) {
      const d = impulse.getChannelData(ch);
      for (let i = 0; i < d.length; i++) d[i] = (Math.random() * 2 - 1) * Math.exp(-3.5 * (i / d.length));
    }
    this.reverb = ctx.createConvolver();
    this.reverb.buffer = impulse;
    this.reverbGain = ctx.createGain();
    this.reverbGain.gain.value = 0.35;
    this.dry = ctx.createGain();
    this.dry.gain.value = 0.8;
    this.bus = ctx.createGain();
    this.bus.connect(this.dry).connect(this.master);
    this.bus.connect(this.reverb).connect(this.reverbGain).connect(this.master);
    this.master.connect(ctx.destination);
  }

  toggle() {
    this.enabled = !this.enabled;
    try { localStorage.setItem(KEY, this.enabled ? 'on' : 'off'); } catch (e) { /* private mode */ }
    this._ensureContext();
    if (this.ctx && this.master) {
      this.master.gain.setTargetAtTime(this.enabled ? 0.5 : 0, this.ctx.currentTime, 0.1);
    }
    return this.enabled;
  }

  /** One enveloped tone. */
  _tone(freq, t0, { a = 0.01, d = 0.12, s = 0.25, r = 0.4, dur = 0.35, gain = 0.25, type = 'sine' } = {}) {
    const ctx = this.ctx;
    const o = ctx.createOscillator();
    o.type = type; o.frequency.value = freq;
    const g = ctx.createGain();
    g.gain.setValueAtTime(0, t0);
    g.gain.linearRampToValueAtTime(gain, t0 + a);                 // attack
    g.gain.linearRampToValueAtTime(gain * s, t0 + a + d);         // decay → sustain
    g.gain.setValueAtTime(gain * s, t0 + dur);
    g.gain.linearRampToValueAtTime(0.0001, t0 + dur + r);         // release
    o.connect(g).connect(this.bus);
    o.start(t0); o.stop(t0 + dur + r + 0.05);
  }

  _play(fn) {
    if (!this.enabled) return;
    this._ensureContext();
    if (!this.ctx) return;
    fn(this.ctx.currentTime + 0.02);
  }

  /** Soft two-note chime — a poll moved. */
  pollChime() {
    this._play((t) => {
      this._tone(660, t, { gain: 0.12, dur: 0.18, type: 'sine' });
      this._tone(880, t + 0.16, { gain: 0.10, dur: 0.25, type: 'sine' });
    });
  }

  /** Short arpeggio — breaking story. */
  storyChime() {
    this._play((t) => {
      [523.25, 659.25, 783.99, 1046.5].forEach((f, i) =>
        this._tone(f, t + i * 0.07, { gain: 0.1, dur: 0.12, r: 0.25, type: 'triangle' }));
    });
  }

  /** Three-note fanfare — a race was called (by a human). */
  callFanfare() {
    this._play((t) => {
      this._tone(392, t, { gain: 0.16, dur: 0.22, type: 'square' });
      this._tone(523.25, t + 0.2, { gain: 0.16, dur: 0.22, type: 'square' });
      this._tone(784, t + 0.42, { gain: 0.18, dur: 0.5, r: 0.8, type: 'square' });
      this._tone(392, t + 0.42, { gain: 0.08, dur: 0.5, r: 0.8, type: 'triangle' });
    });
  }
}
