/** Lightweight synthesized SFX (no asset files) */
(function (global) {
  const LS_KEY = 'tbk_sfx';
  let ctx = null;
  let enabled = true;

  try {
    enabled = localStorage.getItem(LS_KEY) !== '0';
  } catch (e) {
    enabled = true;
  }

  function getCtx() {
    if (!ctx) {
      const AC = global.AudioContext || global.webkitAudioContext;
      if (!AC) return null;
      ctx = new AC();
    }
    return ctx;
  }

  function tone(freq, dur, type, gain) {
    if (!enabled) return;
    const ac = getCtx();
    if (!ac) return;
    if (ac.state === 'suspended') ac.resume().catch(() => {});
    const t0 = ac.currentTime;
    const osc = ac.createOscillator();
    const g = ac.createGain();
    osc.type = type || 'sine';
    osc.frequency.setValueAtTime(freq, t0);
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(gain || 0.08, t0 + 0.01);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    osc.connect(g);
    g.connect(ac.destination);
    osc.start(t0);
    osc.stop(t0 + dur + 0.02);
  }

  const SFX = {
    play() { tone(420, 0.12, 'triangle', 0.07); tone(560, 0.1, 'sine', 0.04); },
    draw() { tone(280, 0.08, 'sine', 0.05); tone(360, 0.1, 'sine', 0.04); },
    discard() { tone(180, 0.1, 'sawtooth', 0.03); },
    damage() { tone(120, 0.16, 'square', 0.06); tone(90, 0.18, 'sawtooth', 0.04); },
    heal() { tone(520, 0.12, 'sine', 0.05); tone(680, 0.14, 'sine', 0.04); },
    equip() { tone(340, 0.1, 'triangle', 0.05); tone(440, 0.12, 'triangle', 0.04); },
    status() { tone(400, 0.1, 'sine', 0.04); },
    ascension() { tone(480, 0.12, 'triangle', 0.06); tone(640, 0.16, 'sine', 0.05); tone(800, 0.2, 'sine', 0.03); },
    die() { tone(80, 0.28, 'sawtooth', 0.05); },
    dying() { tone(150, 0.2, 'triangle', 0.05); },
  };

  function play(name) {
    const fn = SFX[name];
    if (fn) fn();
  }

  function setEnabled(on) {
    enabled = !!on;
    try {
      localStorage.setItem(LS_KEY, enabled ? '1' : '0');
    } catch (e) { /* ignore */ }
  }

  function isEnabled() {
    return enabled;
  }

  global.TBK = global.TBK || {};
  global.TBK.sfx = { play, setEnabled, isEnabled };
})(typeof window !== 'undefined' ? window : globalThis);
