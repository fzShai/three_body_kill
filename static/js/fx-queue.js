/** Serial FX queue driven by snapshot.events[] */
(function (global) {
  function prefersReducedMotion() {
    return !!(global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function rectCenter(el) {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height };
  }

  function wait(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function createFxLayer() {
    let layer = document.getElementById('fxLayer');
    if (!layer) {
      layer = document.createElement('div');
      layer.id = 'fxLayer';
      layer.className = 'fx-layer';
      layer.setAttribute('aria-hidden', 'true');
      document.body.appendChild(layer);
    }
    return layer;
  }

  function flyClone(html, from, to, duration) {
    const layer = createFxLayer();
    const node = document.createElement('div');
    node.className = 'fx-fly';
    node.innerHTML = html;
    const w = from?.w || 96;
    const h = from?.h || 120;
    node.style.width = `${w}px`;
    node.style.height = `${h}px`;
    node.style.left = `${(from?.x || 0) - w / 2}px`;
    node.style.top = `${(from?.y || 0) - h / 2}px`;
    layer.appendChild(node);
    // force layout
    void node.offsetWidth;
    const dx = (to?.x || 0) - (from?.x || 0);
    const dy = (to?.y || 0) - (from?.y || 0);
    node.style.transition = `transform ${duration}ms cubic-bezier(.2,.7,.2,1), opacity ${duration}ms ease`;
    node.style.transform = `translate(${dx}px, ${dy}px) scale(0.92)`;
    node.style.opacity = '0.15';
    return wait(duration).then(() => node.remove());
  }

  function floatText(targetEl, text, kind) {
    if (!targetEl) return wait(0);
    const layer = createFxLayer();
    const anchor = rectCenter(targetEl);
    if (!anchor) return wait(0);
    const node = document.createElement('div');
    node.className = `fx-float fx-float-${kind || 'neutral'}`;
    node.textContent = text;
    node.style.left = `${anchor.x}px`;
    node.style.top = `${anchor.y - 12}px`;
    layer.appendChild(node);
    void node.offsetWidth;
    node.classList.add('fx-float-go');
    return wait(700).then(() => node.remove());
  }

  function flashSeat(targetEl, kind) {
    if (!targetEl) return;
    targetEl.classList.remove('fx-hit', 'fx-heal', 'fx-equip', 'fx-status', 'fx-ascend', 'fx-dying');
    // reflow
    void targetEl.offsetWidth;
    targetEl.classList.add(kind);
    setTimeout(() => targetEl.classList.remove(kind), 520);
  }

  function seatEl(name) {
    return document.querySelector(`.seat-panel[data-user="${CSS.escape(name)}"]`);
  }

  function deckEl() {
    return document.getElementById('deckPile');
  }

  function discardEl() {
    return document.getElementById('discardPile') || document.getElementById('discardCount');
  }

  function stageEl() {
    return document.getElementById('stageCardSlot') || document.getElementById('stage');
  }

  function handCardEl(instanceId) {
    if (!instanceId) return null;
    return document.querySelector(`#hand .card[data-id="${CSS.escape(instanceId)}"]`);
  }

  function handEl() {
    return document.getElementById('hand');
  }

  function toastNear(targetEl, text, kind) {
    if (!targetEl) return wait(0);
    const layer = createFxLayer();
    const anchor = rectCenter(targetEl);
    if (!anchor) return wait(0);
    const node = document.createElement('div');
    node.className = `fx-toast fx-toast-${kind || 'neutral'}`;
    node.textContent = text;
    node.style.left = `${anchor.x}px`;
    node.style.top = `${anchor.y + 28}px`;
    layer.appendChild(node);
    void node.offsetWidth;
    node.classList.add('fx-toast-go');
    return wait(900).then(() => node.remove());
  }

  class FxQueue {
    constructor(opts) {
      this.username = opts.username;
      this.getCardMarkup = opts.getCardMarkup;
      this.queue = [];
      this.running = false;
      this.skip = prefersReducedMotion();
    }

    enqueue(events, meta = {}) {
      if (!events || !events.length) return;
      for (const ev of events) {
        const copy = { ...ev };
        if (meta.fromRects && ev.instance_id && meta.fromRects[ev.instance_id]) {
          copy._fromRect = meta.fromRects[ev.instance_id];
        }
        this.queue.push(copy);
      }
      this._pump();
    }

    _sfx(name) {
      if (global.TBK?.sfx) global.TBK.sfx.play(name);
    }

    async _pump() {
      if (this.running) return;
      this.running = true;
      while (this.queue.length) {
        const batch = this.queue.splice(0, this.queue.length);
        // Parallelize pure feedback (damage/heal) in same tick; serialize card flights
        const flights = [];
        const flashes = [];
        for (const ev of batch) {
          if (['draw', 'play', 'discard'].includes(ev.type)) flights.push(ev);
          else flashes.push(ev);
        }
        await Promise.all(flashes.map((ev) => this._play(ev)));
        for (const ev of flights) {
          await this._play(ev);
        }
      }
      this.running = false;
    }

    async _play(ev) {
      const type = ev.type;
      const reduced = this.skip || prefersReducedMotion();
      const dur = reduced ? 0 : 380;

      if (type === 'damage') {
        const seat = seatEl(ev.target);
        flashSeat(seat, 'fx-hit');
        this._sfx('damage');
        if (!reduced) await floatText(seat, `-${ev.value}`, 'damage');
        return;
      }
      if (type === 'heal') {
        const seat = seatEl(ev.target);
        flashSeat(seat, 'fx-heal');
        this._sfx('heal');
        if (!reduced) await floatText(seat, `+${ev.value}`, 'heal');
        return;
      }
      if (type === 'equip') {
        const seat = seatEl(ev.target || ev.source);
        flashSeat(seat, 'fx-equip');
        this._sfx('equip');
        if (!reduced) await toastNear(seat, `装备 ${ev.card?.name || ''}`, 'equip');
        return;
      }
      if (type === 'status') {
        const seat = seatEl(ev.target);
        flashSeat(seat, 'fx-status');
        this._sfx('status');
        if (!reduced) await toastNear(seat, ev.name || '状态', ev.kind === 'negative' ? 'bad' : 'good');
        return;
      }
      if (type === 'ascension') {
        const seat = seatEl(ev.target);
        flashSeat(seat, 'fx-ascend');
        this._sfx('ascension');
        const label = ev.permanent ? (ev.name || '飞升') : `临时飞升 · ${ev.name || ''}`;
        if (!reduced) await toastNear(seat, label, 'ascend');
        return;
      }
      if (type === 'dying') {
        const seat = seatEl(ev.target);
        flashSeat(seat, 'fx-dying');
        this._sfx('dying');
        return;
      }
      if (type === 'die') {
        const seat = seatEl(ev.target);
        flashSeat(seat, 'fx-dying');
        this._sfx('die');
        if (!reduced) await toastNear(seat, '出局', 'bad');
        return;
      }

      if (type === 'draw') {
        this._sfx('draw');
        if (reduced || dur === 0) return;
        const from = rectCenter(deckEl()) || { x: window.innerWidth / 2, y: 40, w: 72, h: 96 };
        const count = Math.min(Number(ev.count) || 1, 6);
        const hidden = !!ev.hidden || ev.source !== this.username;
        const cards = ev.cards || [];
        const ids = ev.instance_ids || [];
        const destSeat = seatEl(ev.source);
        const toBase = hidden
          ? (rectCenter(destSeat) || from)
          : (rectCenter(handEl()) || rectCenter(destSeat) || from);
        const tasks = [];
        for (let i = 0; i < count; i++) {
          const card = cards[i];
          const html = hidden || !card
            ? this.getCardMarkup(null, { faceDown: true, compact: true })
            : this.getCardMarkup(card, { compact: true, extraClass: 'fx-card-clone' });
          const toEl = !hidden && ids[i] ? handCardEl(ids[i]) : null;
          const to = rectCenter(toEl) || {
            x: toBase.x + i * 12,
            y: toBase.y,
            w: 96,
            h: 120,
          };
          tasks.push(flyClone(html, from, to, dur + i * 40));
        }
        await Promise.all(tasks);
        return;
      }

      if (type === 'play') {
        this._sfx('play');
        if (reduced || dur === 0) return;
        const card = ev.card;
        const html = this.getCardMarkup(card || { name: '?' }, { compact: true, extraClass: 'fx-card-clone' });
        let from = ev._fromRect || null;
        if (!from && ev.source === this.username && ev.instance_id) {
          from = rectCenter(handCardEl(ev.instance_id));
        }
        if (!from) from = rectCenter(seatEl(ev.source)) || { x: window.innerWidth / 2, y: window.innerHeight - 80, w: 96, h: 120 };
        const to = rectCenter(stageEl()) || { x: window.innerWidth / 2, y: window.innerHeight / 2, w: 110, h: 140 };
        await flyClone(html, from, to, dur);
        return;
      }

      if (type === 'discard') {
        this._sfx('discard');
        if (reduced || dur === 0) return;
        const card = ev.card;
        const html = ev.source === this.username && card
          ? this.getCardMarkup(card, { compact: true, extraClass: 'fx-card-clone' })
          : this.getCardMarkup(null, { faceDown: true, compact: true });
        let from = ev._fromRect || null;
        if (!from && ev.source === this.username && ev.instance_id) {
          from = rectCenter(handCardEl(ev.instance_id));
        }
        if (!from) from = rectCenter(seatEl(ev.source)) || rectCenter(handEl());
        const to = rectCenter(discardEl()) || { x: window.innerWidth / 2 + 80, y: 48, w: 72, h: 96 };
        await flyClone(html, from || to, to, dur);
      }
    }
  }

  global.TBK = global.TBK || {};
  global.TBK.FxQueue = FxQueue;
})(typeof window !== 'undefined' ? window : globalThis);
