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

  function cardTopMeta(c) {
    const kind = cardKind(c);
    if (kind === 'kill' || kind === 'dodge') return `${c.tier || '?'}阶`;
    if (kind === 'heal') return `+${c.heal || 2}`;
    if (kind === 'equipment') return SLOT_LABEL[c.slot] || '装备';
    if (kind === 'ascend') return '飞升';
    if (kind === 'visitor') return '基本';
    if (kind === 'trick') return '锦囊';
    if (kind === 'realm') return '虚境';
    if (kind === 'field') return '场地';
    return cardTypeLabel(c) || '牌';
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
    const selected = opts.selected ? ' selected' : '';
    const dim = opts.dim ? ' dim' : '';
    const compact = opts.compact ? ' card-compact' : '';
    const extra = opts.extraClass ? ` ${opts.extraClass}` : '';
    const iid = escHtml(c?.instance_id || '');
    const subtype = escHtml(c?.subtype || '');
    const cid = escHtml(c?.id || '');
    const name = escHtml(c?.name || '?');
    const typeLabel = escHtml(cardTypeLabel(c));
    const top = escHtml(cardTopMeta(c));
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
    const body = opts.compact
      ? `<div class="card-top"><span class="card-meta">${top}</span></div>
         <div class="cname">${name}</div>
         <div class="ctype">${typeLabel}</div>`
      : `<div class="card-top"><span class="card-meta">${top}</span></div>
         <div class="cname">${name}</div>
         <div class="card-art" aria-hidden="true"><span class="card-glyph">${glyph}</span></div>
         <div class="ctype">${typeLabel}</div>
         <div class="ctext">${text}</div>`;
    return `<div class="card card-face card-lift card-kind-${kind}${selected}${dim}${compact}${extra}" data-id="${iid}" data-subtype="${subtype}" data-card-id="${cid}" data-kind="${kind}"${style}>
      ${body}
    </div>`;
  }

  global.TBK = global.TBK || {};
  global.TBK.cardMarkup = cardMarkup;
  global.TBK.cardKind = cardKind;
  global.TBK.cardTypeLabel = cardTypeLabel;
  global.TBK.TEMP_ASCEND_IDS = TEMP_ASCEND_IDS;
  global.TBK.SLOT_LABEL = SLOT_LABEL;
})(typeof window !== 'undefined' ? window : globalThis);
