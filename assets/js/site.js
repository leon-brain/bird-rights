/* Bird Rights Keeper League — меню, выбор матчап-периода, раскрытие категорий. */
(function () {
  'use strict';

  // --- мобильное меню
  var toggle = document.querySelector('.nav-toggle');
  var nav = document.getElementById('site-nav');
  if (toggle && nav) {
    var setOpen = function (open) {
      nav.classList.toggle('is-open', open);
      toggle.setAttribute('aria-expanded', String(open));
    };
    toggle.addEventListener('click', function () {
      setOpen(toggle.getAttribute('aria-expanded') !== 'true');
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && toggle.getAttribute('aria-expanded') === 'true') { setOpen(false); toggle.focus(); }
    });
    document.addEventListener('click', function (e) {
      if (!nav.contains(e.target) && !toggle.contains(e.target)) setOpen(false);
    });
  }

  // --- раскрытие категорий в карточке матчапа (на телефоне)
  document.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('.mu-toggle') : null;
    if (!btn) return;
    var box = document.getElementById(btn.getAttribute('aria-controls'));
    if (!box) return;
    var open = btn.getAttribute('aria-expanded') !== 'true';
    btn.setAttribute('aria-expanded', String(open));
    box.classList.toggle('is-open', open);
  });

  // --- выбор матчап-периода
  var sections = Array.prototype.slice.call(document.querySelectorAll('[data-period-section]'));
  if (sections.length) {
    var select = document.getElementById('period-select');
    var chips = Array.prototype.slice.call(document.querySelectorAll('.chip[data-period]'));
    var steps = Array.prototype.slice.call(document.querySelectorAll('.period-step'));
    var ids = sections.map(function (s) { return s.id; });
    var show = function (id, updateHash) {
      if (ids.indexOf(id) === -1) return false;
      sections.forEach(function (s) { s.classList.toggle('is-active', s.id === id); });
      chips.forEach(function (c) {
        if (c.getAttribute('data-period') === id) c.setAttribute('aria-current', 'true');
        else c.removeAttribute('aria-current');
      });
      if (select) select.value = id;
      var i = ids.indexOf(id);
      steps.forEach(function (b) {
        var next = i + Number(b.getAttribute('data-step'));
        b.disabled = next < 0 || next >= ids.length;
      });
      if (updateHash && history.replaceState) history.replaceState(null, '', '#' + id);
      return true;
    };
    var current = function () {
      var active = document.querySelector('[data-period-section].is-active');
      return active ? active.id : ids[0];
    };
    if (select) select.addEventListener('change', function () { show(select.value, true); });
    chips.forEach(function (c) {
      c.addEventListener('click', function (e) { e.preventDefault(); show(c.getAttribute('data-period'), true); });
    });
    steps.forEach(function (b) {
      b.addEventListener('click', function () {
        var next = ids[ids.indexOf(current()) + Number(b.getAttribute('data-step'))];
        if (next) show(next, true);
      });
    });
    window.addEventListener('hashchange', function () { show(location.hash.slice(1), false); });
    if (!show(location.hash.slice(1), false)) show(current(), false);
  }

  // --- сортировка таблиц по столбцам
  Array.prototype.forEach.call(document.querySelectorAll('table[data-sortable]'), function (table) {
    var heads = Array.prototype.slice.call(table.tHead.rows[0].cells);
    var body = table.tBodies[0];
    var value = function (row, i) {
      var cell = row.cells[i];
      if (cell.hasAttribute('data-v')) return parseFloat(cell.getAttribute('data-v'));
      return (cell.getAttribute('data-t') || cell.textContent).trim().toLowerCase();
    };
    heads.forEach(function (th, i) {
      var btn = th.querySelector('.sort');
      if (!btn) return;
      btn.addEventListener('click', function () {
        var current = th.getAttribute('aria-sort');
        var dir = current ? (current === 'ascending' ? 'descending' : 'ascending')
                          : (th.getAttribute('data-dir') === 'asc' ? 'ascending' : 'descending');
        var sign = dir === 'ascending' ? 1 : -1;
        var rows = Array.prototype.slice.call(body.rows);
        rows.sort(function (a, b) {
          var x = value(a, i), y = value(b, i), d;
          d = (typeof x === 'number' && typeof y === 'number') ? x - y : String(x).localeCompare(String(y), 'ru');
          return d ? d * sign : Number(a.getAttribute('data-rank')) - Number(b.getAttribute('data-rank'));
        });
        rows.forEach(function (r) { body.appendChild(r); });
        heads.forEach(function (h) { h.removeAttribute('aria-sort'); });
        th.setAttribute('aria-sort', dir);
      });
    });
    if (heads[0]) heads[0].setAttribute('aria-sort', 'ascending');
  });

  // --- оглавление устава: на телефоне свёрнуто, после перехода закрывается
  var toc = document.getElementById('toc');
  if (toc && window.matchMedia) {
    var narrow = window.matchMedia('(max-width: 999.98px)');
    var sync = function () { toc.open = !narrow.matches; };
    sync();
    if (narrow.addEventListener) narrow.addEventListener('change', sync);
    toc.addEventListener('click', function (e) {
      if (narrow.matches && e.target.closest && e.target.closest('a')) toc.open = false;
    });
  }
})();
