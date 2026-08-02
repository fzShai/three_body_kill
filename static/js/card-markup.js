/** Shared card face markup for hand / stage / FX clones */
(function (global) {
  const SLOT_LABEL = {
    stellar_track: '恒星航迹',
    stability_system: '维稳系统',
    ship: '船',
    armor: '甲',
    temp_ascend: '临时飞升',
  };

  const TEMP_ASCEND_IDS = new Set(['nano_center', 'chip_workshop', 'stars_plan']);

  const TYPE_ICON = {
    kill: '杀',
    dodge: '闪',
    heal: '桃',
    visitor: '客',
    trick: '囊',
    equipment: '装',
    realm: '境',
    field: '场',
    ascend: '升',
    back: '?',
  };

  function escHtml(s) {
    return String(s ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function cardKind(c) {
    if (!c) return 'unknown';
    if (c.subtype === 'kill') return 'kill';
    if (c.subtype === 'dodge') return 'dodge';
    if (c.subtype === 'heal' || c.id === 'peach') return 'heal';
    if (c.subtype === 'visitor' || c.id === 'visitor') return 'visitor';
    if (TEMP_ASCEND_IDS.has(c.id) || c.slot === 'temp_ascend') return 'ascend';
    if (c.type === 'equipment' || c.slot === 'ship' || c.slot === 'armor') return 'equipment';
    if (c.type === 'realm' || c.realm_id) return 'realm';
    if (c.type === 'field') return 'field';
    if (c.type === 'trick' || c.subtype === 'trick') return 'trick';
    return c.type || 'unknown';
  }

  /** Mirror of pools.json tech_pool_max — client fallback from pool_entry. */
  const TECH_POOL_MAX = { 1: 22, 2: 30, 3: 43, 4: 53, 5: 74, 6: 80 };

  function clampRank(n) {
    const v = Number(n);
    if (!Number.isFinite(v)) return 1;
    return Math.max(1, Math.min(6, Math.round(v)));
  }

  function techForEntry(entry) {
    const e = Number(entry);
    if (!Number.isFinite(e) || e <= 0) return 1;
    for (let t = 1; t <= 6; t += 1) {
      if (e <= TECH_POOL_MAX[t]) return t;
    }
    return 6;
  }

  /**
   * Kill/dodge → 阶 (visual_tier or tier).
   * Others → visual_tier, else pool_entry→科等.
   */
  function cardVisualTier(c) {
    if (!c) return 1;
    const isKd = c.subtype === 'kill' || c.subtype === 'dodge';
    if (isKd) {
      if (c.visual_tier != null && c.visual_tier !== '') return clampRank(c.visual_tier);
      return clampRank(c.tier || 1);
    }
    if (c.visual_tier != null && c.visual_tier !== '') return clampRank(c.visual_tier);
    if (c.pool_entry != null && c.pool_entry !== '') return techForEntry(c.pool_entry);
    return 1;
  }

  function cardBand(rank) {
    if (rank <= 2) return 'low';
    if (rank <= 4) return 'mid';
    return 'high';
  }

  function cardTypeLabel(c) {
    if (!c) return '';
    if (c.subtype === 'kill') return `${c.tier || '?'}阶杀`;
    if (c.subtype === 'dodge') return `${c.tier || '?'}阶闪`;
    if (c.subtype === 'heal' || c.id === 'peach') return `桃 · 回复${c.heal || 2}`;
    if (c.subtype === 'visitor' || c.id === 'visitor') return '基本 · 天外来客';
    if (TEMP_ASCEND_IDS.has(c.id) || c.slot === 'temp_ascend') return '临时飞升';
    if (c.type === 'equipment') return `装备 · ${SLOT_LABEL[c.slot] || c.slot || ''}`;
    if (c.type === 'realm') return '虚境';
    if (c.type === 'field') return '场地';
    if (c.type === 'trick') return '锦囊';
    return c.type || '牌';
  }

  function cardTopMeta(c, rank) {
    const kind = cardKind(c);
    if (kind === 'kill' || kind === 'dodge') return `${c.tier || rank}阶`;
    return `科${rank}`;
  }

  function cardArtGlyph(c) {
    return TYPE_ICON[cardKind(c)] || '牌';
  }

  /**
   * @param {object} c card
   * @param {object} opts
   * @param {boolean} [opts.selected]
   * @param {boolean} [opts.dim]
   * @param {boolean} [opts.compact] stage / flying clone
   * @param {boolean} [opts.faceDown]
   * @param {string} [opts.extraClass]
   * @param {number} [opts.fanIndex]
   * @param {number} [opts.fanTotal]
   */
  function cardMarkup(c, opts = {}) {
    if (opts.faceDown) {
      return `<div class="card card-face card-back ${opts.extraClass || ''}" data-face="back">
        <div class="card-back-mark">三体</div>
      </div>`;
    }
    const kind = cardKind(c);
    const rank = cardVisualTier(c);
    const band = cardBand(rank);
    const selected = opts.selected ? ' selected' : '';
    const dim = opts.dim ? ' dim' : '';
    const compact = opts.compact ? ' card-compact' : '';
    const extra = opts.extraClass ? ` ${opts.extraClass}` : '';
    const iid = escHtml(c?.instance_id || '');
    const subtype = escHtml(c?.subtype || '');
    const cid = escHtml(c?.id || '');
    const name = escHtml(c?.name || '?');
    const typeLabel = escHtml(cardTypeLabel(c));
    const top = escHtml(cardTopMeta(c, rank));
    const text = escHtml(c?.text || '');
    const glyph = escHtml(cardArtGlyph(c));
    const styleBits = [];
    if (typeof opts.fanIndex === 'number' && opts.fanTotal > 1) {
      const mid = (opts.fanTotal - 1) / 2;
      const rot = (opts.fanIndex - mid) * 2.2;
      const lift = Math.abs(opts.fanIndex - mid) * 1.2;
      styleBits.push(`--fan-rot:${rot.toFixed(2)}deg`);
      styleBits.push(`--fan-lift:${lift.toFixed(1)}px`);
    }
    const style = styleBits.length ? ` style="${styleBits.join(';')}"` : '';
    const corners = band === 'high'
      ? (rank >= 6
        ? `<span class="card-corner card-corner-tl" aria-hidden="true"></span>
           <span class="card-corner card-corner-tr" aria-hidden="true"></span>
           <span class="card-corner card-corner-bl" aria-hidden="true"></span>
           <span class="card-corner card-corner-br" aria-hidden="true"></span>`
        : `<span class="card-corner card-corner-tl" aria-hidden="true"></span>
           <span class="card-corner card-corner-br" aria-hidden="true"></span>`)
      : '';
    const topBar = `<div class="card-top">
        <span class="card-rank-pip" title="${top}" aria-hidden="true"></span>
        <span class="card-meta">${top}</span>
      </div>`;
    const body = opts.compact
      ? `${corners}${topBar}
         <div class="cname">${name}</div>
         <div class="ctype">${typeLabel}</div>`
      : `${corners}${topBar}
         <div class="cname">${name}</div>
         <div class="card-art" aria-hidden="true"><span class="card-glyph">${glyph}</span></div>
         <div class="ctype">${typeLabel}</div>
         <div class="ctext">${text}</div>`;
    return `<div class="card card-face card-lift card-kind-${kind} card-rank-${rank} card-band-${band}${selected}${dim}${compact}${extra}" data-id="${iid}" data-subtype="${subtype}" data-card-id="${cid}" data-kind="${kind}" data-rank="${rank}"${style}>
      ${body}
    </div>`;
  }

  global.TBK = global.TBK || {};
  global.TBK.cardMarkup = cardMarkup;
  global.TBK.cardKind = cardKind;
  global.TBK.cardTypeLabel = cardTypeLabel;
  global.TBK.cardVisualTier = cardVisualTier;
  global.TBK.TEMP_ASCEND_IDS = TEMP_ASCEND_IDS;
  global.TBK.SLOT_LABEL = SLOT_LABEL;
})(typeof window !== 'undefined' ? window : globalThis);
