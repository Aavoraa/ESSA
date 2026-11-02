/* ==========================================================================
   One screen, no scrolling — the shop's side of it
   --------------------------------------------------------------------------
   Every page is fitted to the window instead of scrolling:

   1. A long table shows as many rows as there is room for, with a slim pager
      under it (Previous · Page 2 of 5 · Next).
   2. Sections are NOT split into pages any more (that was asked to stop): a
      form or screen shows everything at once. flowSections() is kept but unused.

   Nothing is removed and nothing reloads: rows and sections are only hidden on
   screen (class fit-hide), so a form still submits every field, a search still
   sees every row, and printing shows everything. A required field that is on a
   hidden page is brought into view when the form is submitted.

   The billing counter and settlement lay themselves out for one screen already
   and are left alone.
   ========================================================================== */
(function () {
  'use strict';
  // the path inside the shop, whether it runs on its own or mounted at /pos
  var root = typeof SHOP_ROOT !== 'undefined' ? SHOP_ROOT : '';
  var rel = location.pathname.slice(root.length) || '/';
  if (/^\/pos\/(settle\/?)?$/.test(rel)) {
    // The counter and settlement are one fixed screen already. Their height
    // was a guess (100vh − 150px) that came out a few pixels long inside the
    // warehouse frame, so it is measured from where the screen actually starts.
    var size = function () {
      var ps = document.querySelector('.pos-screen');
      if (!ps) return;
      if (window.innerWidth < 992) { ps.style.height = ''; return; }   // stacked: it grows
      var m = document.querySelector('main');
      var padB = m ? parseFloat(getComputedStyle(m).paddingBottom) || 0 : 0;
      ps.style.height = Math.max(320, window.innerHeight - ps.getBoundingClientRect().top - window.scrollY - padB) + 'px';
    };
    window.addEventListener('resize', size);
    if (document.readyState === 'complete') size(); else window.addEventListener('load', size);
    return;
  }
  var main = document.querySelector('main');
  if (!main) return;

  var BAR = 56;                  // a pager or page bar, with its margin
  var pagers = [];
  var flows = [];
  var busy = false;

  function over() { return document.documentElement.scrollHeight - window.innerHeight; }
  function shown(el) { return el.getClientRects().length > 0; }
  function hide(el, on) { el.classList.toggle('fit-hide', !!on); }
  function outerH(el) {
    var cs = getComputedStyle(el);
    return el.getBoundingClientRect().height + (parseFloat(cs.marginTop) || 0) + (parseFloat(cs.marginBottom) || 0);
  }

  // ---- 1. tables -----------------------------------------------------------
  function render(p) {
    var n = p.rows.length;
    var pages = Math.max(1, Math.ceil(n / p.size));
    p.page = Math.min(Math.max(0, p.page), pages - 1);
    var a = p.page * p.size;
    var b = Math.min(n, a + p.size);
    p.rows.forEach(function (r, i) { hide(r, i < a || i >= b); });
    if (pages <= 1) { p.bar.hidden = true; p.bar.innerHTML = ''; return; }
    p.bar.hidden = false;
    p.bar.innerHTML =
      '<span class="fit-count">' + (a + 1) + '–' + b + ' of ' + n + '</span>' +
      '<button type="button" class="btn btn-sm btn-outline-secondary" data-step="-1"' + (p.page ? '' : ' disabled') + '>‹ Previous</button>' +
      '<span>Page ' + (p.page + 1) + ' of ' + pages + '</span>' +
      '<button type="button" class="btn btn-sm btn-outline-secondary" data-step="1"' + (p.page < pages - 1 ? '' : ' disabled') + '>Next ›</button>';
  }
  function collectTables() {
    [].forEach.call(main.querySelectorAll('table'), function (t) {
      if (t.dataset.fitDone || t.closest('[data-fit="off"]') || !t.tBodies[0]) return;
      var rows = [].slice.call(t.tBodies[0].rows);
      if (rows.length < 4) return;
      t.dataset.fitDone = '1';
      var host = t.closest('.table-responsive') || t;
      var bar = document.createElement('div');
      bar.className = 'fit-pager';
      bar.hidden = true;
      host.parentNode.insertBefore(bar, host.nextSibling);
      var p = { table: t, rows: rows, size: rows.length, page: 0, bar: bar };
      bar.addEventListener('click', function (e) {
        var btn = e.target.closest('button[data-step]');
        if (!btn) return;
        p.page += +btn.dataset.step;
        render(p);
      });
      pagers.push(p);
    });
  }
  function fitTables() {
    pagers.slice().sort(function (x, y) { return y.rows.length - x.rows.length; }).forEach(function (p) {
      var o = over();
      if (o <= 0 || !shown(p.table)) return;
      var vis = p.rows.filter(function (r) { return !r.classList.contains('fit-hide'); });
      var h = 0;
      vis.forEach(function (r) { h += r.getBoundingClientRect().height; });
      h = h / (vis.length || 1);
      if (!(h > 4)) return;
      var newBar = p.bar.hidden ? BAR : 0;
      p.size = Math.max(3, Math.min(p.size, vis.length) - Math.ceil((o + newBar) / h));
      render(p);
    });
  }
  // a table that scrolls inside its own box is sized to that box
  function fitBoxedTables() {
    pagers.forEach(function (p) {
      var host = p.table.closest('.table-responsive');
      if (!host || !shown(p.table) || host.scrollHeight <= host.clientHeight + 2) return;
      var oy = getComputedStyle(host).overflowY;
      if (oy !== 'auto' && oy !== 'scroll') return;
      var vis = p.rows.filter(function (r) { return !r.classList.contains('fit-hide'); });
      var h = 0;
      vis.forEach(function (r) { h += r.getBoundingClientRect().height; });
      h = h / (vis.length || 1);
      var head = p.table.tHead ? p.table.tHead.getBoundingClientRect().height : 0;
      if (!(h > 4)) return;
      p.size = Math.max(3, Math.floor((host.clientHeight - head) / h));
      render(p);
    });
  }

  // ---- 2. sections -----------------------------------------------------------
  function blocksOf(root) {
    return [].filter.call(root.children, function (el) {
      if (/^(SCRIPT|TEMPLATE|STYLE|DATALIST|INPUT)$/.test(el.tagName)) return false;
      if (el.classList.contains('modal') || el.classList.contains('fit-bar') ||
          el.classList.contains('page-head') || el.classList.contains('fit-pager')) return false;
      var pos = getComputedStyle(el).position;
      return pos !== 'fixed' && pos !== 'absolute' && shown(el);
    });
  }
  // side by side, these cannot be stacked into pages without changing the layout
  function stacked(kids) {
    for (var i = 1; i < kids.length; i++) {
      if (Math.abs(kids[i].getBoundingClientRect().top - kids[i - 1].getBoundingClientRect().top) < 4) return false;
    }
    return true;
  }
  // Where the page's sections are. Down through wrappers that hold just one
  // thing (a form, a row); a stack of sections is paged as one; columns side by
  // side are each paged on their own. A card is one section, never split.
  function pickRoots(el, depth) {
    if (depth > 5 || el.classList.contains('card')) return [];
    var kids = blocksOf(el);
    if (kids.length === 1) return pickRoots(kids[0], depth + 1);
    if (kids.length < 2) return [];
    if (stacked(kids)) return [el];
    var out = [];
    kids.forEach(function (k) { out = out.concat(pickRoots(k, depth + 1)); });
    return out;
  }
  function clearFlow() {
    flows.forEach(function (f) {
      f.blocks.forEach(function (b) { hide(b, false); });
      f.bar.remove();
    });
    flows = [];
  }
  function flowIn(root) {
    var blocks = blocksOf(root);
    if (blocks.length < 2) return;
    var top = blocks[0].getBoundingClientRect().top + window.scrollY;
    var below = document.documentElement.scrollHeight - (root.getBoundingClientRect().bottom + window.scrollY);
    var avail = window.innerHeight - top - below - BAR;
    var groups = [];
    var cur = [];
    var h = 0;
    blocks.forEach(function (b) {
      var bh = outerH(b);
      if (cur.length && h + bh > avail) { groups.push(cur); cur = []; h = 0; }
      cur.push(b);
      h += bh;
    });
    if (cur.length) groups.push(cur);
    if (groups.length < 2) return;
    var bar = document.createElement('nav');
    bar.className = 'fit-bar';
    bar.setAttribute('aria-label', 'Pages of this section');
    root.appendChild(bar);
    var f = { root: root, blocks: blocks, groups: groups, page: 0, bar: bar };
    flows.push(f);
    bar.addEventListener('click', function (e) {
      var btn = e.target.closest('button[data-page]');
      if (btn) showPage(f, +btn.dataset.page);
    });
    showPage(f, 0);
  }
  function flowSections() {
    pickRoots(main, 0).forEach(flowIn);
  }
  function title(b) {
    var h = b.querySelector('h1, h2, h3, h4, h5, h6, .card-header, legend');
    var t = h ? h.textContent.trim().replace(/\s+/g, ' ') : '';
    return t.length > 26 ? t.slice(0, 25) + '…' : t;
  }
  function showPage(f, i) {
    f.page = Math.max(0, Math.min(i, f.groups.length - 1));
    f.blocks.forEach(function (b) { hide(b, f.groups[f.page].indexOf(b) < 0); });
    var html = '<button type="button" class="btn btn-sm btn-outline-secondary" data-page="' + (f.page - 1) + '"' + (f.page ? '' : ' disabled') + ' aria-label="Previous page">‹</button><span class="fit-pages">';
    f.groups.forEach(function (g, k) {
      var t = title(g[0]);
      html += '<button type="button" class="fit-pg' + (k === f.page ? ' on' : '') + '" data-page="' + k + '"' +
        (k === f.page ? ' aria-current="page"' : '') + '><b>' + (k + 1) + '</b>' + (t ? ' ' + t.replace(/</g, '&lt;') : '') + '</button>';
    });
    html += '</span><button type="button" class="btn btn-sm btn-outline-secondary" data-page="' + (f.page + 1) + '"' + (f.page < f.groups.length - 1 ? '' : ' disabled') + ' aria-label="Next page">›</button>';
    f.bar.innerHTML = html;
    window.scrollTo(0, 0);
  }

  // ---- the whole fit, from scratch ---------------------------------------------
  function fit() {
    if (busy) return;
    busy = true;
    try {
      clearFlow();
      collectTables();
      pagers.forEach(function (p) { p.size = p.rows.length; p.page = 0; render(p); });
      fitBoxedTables();
      if (over() > 0) fitTables();
      // Sections are no longer split into pages: every screen shows all of its
      // fields at once and scrolls if it must. Only long tables are paged.
    } finally { busy = false; }
  }

  // a required field on a hidden page or row is shown when the form is submitted
  document.addEventListener('invalid', function (e) {
    var el = e.target;
    flows.forEach(function (f) {
      for (var k = 0; k < f.groups.length; k++) {
        if (f.groups[k].some(function (b) { return b.contains(el); })) { showPage(f, k); break; }
      }
    });
    pagers.forEach(function (p) {
      var i = p.rows.findIndex(function (r) { return r.contains(el); });
      if (i >= 0) { p.page = Math.floor(i / p.size); render(p); }
    });
  }, true);

  var t;
  function later(ms) { clearTimeout(t); t = setTimeout(fit, ms); }
  window.addEventListener('resize', function () { later(200); });
  // pages that build their content after loading (fetch, templates) are fitted again
  // (its own pagers and bars coming and going are not a change of content)
  function ours(n) {
    return n.nodeType === 1 && (n.classList.contains('fit-pager') || n.classList.contains('fit-bar'));
  }
  new MutationObserver(function (recs) {
    for (var i = 0; i < recs.length; i++) {
      var r = recs[i];
      var n = r.target;
      if (n.closest && (n.closest('.fit-pager') || n.closest('.fit-bar'))) continue;
      var nodes = [].slice.call(r.addedNodes).concat([].slice.call(r.removedNodes));
      if (nodes.length && nodes.every(ours)) continue;
      later(300);
      return;
    }
  }).observe(main, { childList: true, subtree: true });
  if (document.readyState === 'complete') fit();
  else window.addEventListener('load', fit);
  window.shopFit = fit;
})();
