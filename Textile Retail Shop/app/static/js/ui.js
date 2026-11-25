/* ==========================================================================
   ESSA AI design system — shop behaviours
   --------------------------------------------------------------------------
   uiConfirm(): the in-app replacement for window.confirm, with the same
   yes / no outcome, drawn with the shared .ui- modal styles.

     if (!(await uiConfirm({ title: 'Delete coupon?', message: '…', danger: true }))) return;

   Declarative form for links, buttons and forms that only need to ask first:
     <form data-ui-confirm="Delete this coupon?" data-ui-danger> … </form>
     <a href="…" data-ui-confirm="Close the drawer?">…</a>
   ========================================================================== */
(function () {
  const ICONS = (document.currentScript && document.currentScript.dataset.sprite) || '';
  const icon = (name, size) =>
    `<svg class="ui-icon" width="${size}" height="${size}" aria-hidden="true"><use href="${ICONS}#i-${name}"/></svg>`;
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  window.uiConfirm = function (opts) {
    const o = typeof opts === 'string' ? { message: opts } : (opts || {});
    return new Promise((resolve) => {
      const back = document.createElement('div');
      back.className = 'ui-backdrop ui';
      back.innerHTML = `
        <div class="ui-modal ui-modal--sm" role="dialog" aria-modal="true" aria-label="${esc(o.title || 'Please confirm')}">
          <div class="ui-modal__head"><h2 class="ui-modal__title">${esc(o.title || 'Please confirm')}</h2></div>
          <div class="ui-modal__body"><div class="ui-confirm${o.danger ? ' ui-confirm--danger' : ''}">
            <span class="ui-confirm__icon">${icon(o.danger ? 'alerttri' : 'info', 20)}</span>
            <p class="ui-confirm__text">${esc(o.message)}</p></div></div>
          <div class="ui-modal__foot">
            <button type="button" class="ui-btn" data-a="no">${esc(o.cancelLabel || 'Cancel')}</button>
            <button type="button" class="ui-btn ${o.danger ? 'ui-btn--danger' : 'ui-btn--primary'}" data-a="yes">${esc(o.confirmLabel || 'Confirm')}</button>
          </div></div>`;
      const done = (v) => { document.removeEventListener('keydown', key, true); back.remove(); resolve(v); };
      const key = (e) => { if (e.key === 'Escape') { e.stopPropagation(); done(false); } };
      back.addEventListener('mousedown', (e) => { if (e.target === back) done(false); });
      back.querySelector('[data-a=no]').onclick = () => done(false);
      back.querySelector('[data-a=yes]').onclick = () => done(true);
      document.addEventListener('keydown', key, true);
      document.body.appendChild(back);
      back.querySelector('[data-a=yes]').focus();
    });
  };

  // data-ui-confirm on a form, link or button
  document.addEventListener('submit', async (e) => {
    const f = e.target.closest('form[data-ui-confirm]');
    if (!f || f.dataset.uiConfirmed) return;
    e.preventDefault();
    if (await uiConfirm({ message: f.dataset.uiConfirm, danger: 'uiDanger' in f.dataset })) {
      f.dataset.uiConfirmed = '1';
      (e.submitter && f.requestSubmit) ? f.requestSubmit(e.submitter) : f.submit();
    }
  }, true);
  document.addEventListener('click', async (e) => {
    const el = e.target.closest('a[data-ui-confirm], button[data-ui-confirm]:not([type=submit])');
    if (!el || el.dataset.uiConfirmed) return;
    e.preventDefault(); e.stopPropagation();
    if (await uiConfirm({ message: el.dataset.uiConfirm, danger: 'uiDanger' in el.dataset })) {
      el.dataset.uiConfirmed = '1';
      el.click();
      delete el.dataset.uiConfirmed;
    }
  }, true);
})();
