// Table behaviour: click-to-sort, filter selects, and pagination.
//
// One module because the three decide the same thing — which rows are on screen.
// A filter says which rows are eligible, pagination says which of those fit on the
// current page, and sorting reorders them. Split across files they fight over the
// `hidden` property and rows vanish that shouldn't, so `apply()` is the single
// owner of visibility and everything else just asks it to run again.

(function () {
  "use strict";

  var SIZES = [10, 15, 25, 50];
  var DEFAULT_SIZE = 10;
  var state = {};

  function key(table) {
    return "nw.pagesize." + (table.id || "table");
  }

  function settings(table) {
    var id = table.id || "table";
    if (!state[id]) {
      var size = parseInt(table.dataset.paginate, 10) || DEFAULT_SIZE;
      try {
        var saved = parseInt(window.localStorage.getItem(key(table)), 10);
        if (SIZES.indexOf(saved) !== -1) size = saved;
      } catch (e) {
        // Private browsing can refuse localStorage; the default is fine.
      }
      state[id] = { page: 1, size: size };
    }
    return state[id];
  }

  // Rows marked data-attached (a holding's lot-ledger row) belong to the row above
  // them: they're never counted, paged or unhidden on their own, and a sort carries
  // them along with their parent.
  function bodyRows(table) {
    if (!table.tBodies.length) return [];
    return Array.prototype.slice.call(table.tBodies[0].rows).filter(function (r) {
      return !r.dataset.empty && r.dataset.attached === undefined;
    });
  }

  function attachedTo(row) {
    var out = [];
    var next = row.nextElementSibling;
    while (next && next.dataset.attached !== undefined) {
      out.push(next);
      next = next.nextElementSibling;
    }
    return out;
  }

  function controlsFor(table) {
    return document.querySelectorAll('[data-filter-table="' + table.id + '"]');
  }

  // Every filter targeting this table has to agree, or picking a holding would
  // quietly undo the buys/sells filter chosen a moment earlier.
  function matches(table, row) {
    var ok = true;
    Array.prototype.forEach.call(controlsFor(table), function (sel) {
      if (!ok || !sel.value) return;
      if (row.dataset[sel.dataset.filterKey] !== sel.value) ok = false;
    });
    return ok;
  }

  function pageList(current, total) {
    var out = [];
    for (var i = 1; i <= total; i++) {
      if (i === 1 || i === total || Math.abs(i - current) <= 1) {
        out.push(i);
      } else if (out[out.length - 1] !== "…") {
        out.push("…");
      }
    }
    return out;
  }

  function pagerFor(table) {
    var host = table.closest(".table-scroll") || table;
    var next = host.nextElementSibling;
    if (next && next.classList && next.classList.contains("pager")) return next;
    var el = document.createElement("div");
    el.className = "pager";
    el.dataset.pagerFor = table.id;
    host.parentNode.insertBefore(el, host.nextSibling);
    return el;
  }

  function renderPager(table, total, pages) {
    var st = settings(table);
    var el = pagerFor(table);

    // Nothing to page through and no choice worth offering.
    if (total <= SIZES[0] && st.size === (parseInt(table.dataset.paginate, 10) || DEFAULT_SIZE)) {
      // Emptied as well as hidden: a stale "1–10 of 82" left in the DOM is still
      // read out by a screen reader even when nothing is on screen.
      el.hidden = true;
      el.innerHTML = "";
      return;
    }
    el.hidden = false;

    var first = total ? (st.page - 1) * st.size + 1 : 0;
    var last = Math.min(st.page * st.size, total);

    var html = '<label class="pager-size">Show '
      + '<select data-page-size="' + table.id + '" aria-label="Rows per page">'
      + SIZES.map(function (n) {
          return '<option value="' + n + '"' + (n === st.size ? " selected" : "") + ">" + n + "</option>";
        }).join("")
      + "</select></label>"
      + '<span class="pager-info">' + first + "–" + last + " of " + total + "</span>";

    if (pages > 1) {
      html += '<span class="pager-nav">';
      html += '<button type="button" class="ghost tiny" data-page="' + (st.page - 1) + '"'
        + (st.page === 1 ? " disabled" : "") + ' aria-label="Previous page">‹</button>';
      pageList(st.page, pages).forEach(function (p) {
        if (p === "…") {
          html += '<span class="pager-gap">…</span>';
        } else {
          html += '<button type="button" class="ghost tiny'
            + (p === st.page ? " current" : "") + '" data-page="' + p + '"'
            + (p === st.page ? ' aria-current="page"' : "") + ">" + p + "</button>";
        }
      });
      html += '<button type="button" class="ghost tiny" data-page="' + (st.page + 1) + '"'
        + (st.page === pages ? " disabled" : "") + ' aria-label="Next page">›</button>';
      html += "</span>";
    }
    el.innerHTML = html;
  }

  function apply(table) {
    var rows = bodyRows(table);
    var eligible = [];
    rows.forEach(function (r) {
      if (matches(table, r)) {
        eligible.push(r);
      } else {
        r.hidden = true;
      }
    });

    var count = document.querySelector('[data-filter-count="' + table.id + '"]');
    if (count) count.textContent = eligible.length;

    if (table.dataset.paginate === undefined) {
      eligible.forEach(function (r) { r.hidden = false; });
      return;
    }

    var st = settings(table);
    var pages = Math.max(1, Math.ceil(eligible.length / st.size));
    if (st.page > pages) st.page = pages;
    if (st.page < 1) st.page = 1;

    var start = (st.page - 1) * st.size;
    eligible.forEach(function (r, i) {
      r.hidden = i < start || i >= start + st.size;
    });

    renderPager(table, eligible.length, pages);
  }

  function applyAll() {
    Array.prototype.forEach.call(
      document.querySelectorAll("table[data-paginate], table[id]"),
      function (t) { if (t.id) apply(t); }
    );
  }

  // ---- sorting ----------------------------------------------------------

  var MISSING = null;

  // Reads what the cell shows *now*: an inline-edited amount lives in an <input>, a
  // bucket in a <select>, and a USD figure is rewritten in place after a save, so
  // nothing is cached from page load.
  function cellValue(row, index, kind) {
    var td = row.children[index];
    if (!td) return MISSING;
    var raw;
    var field = td.querySelector("input, select");
    if (td.dataset.value !== undefined) {
      raw = td.dataset.value;
    } else if (field && field.tagName === "SELECT") {
      raw = field.selectedIndex > 0 ? field.options[field.selectedIndex].text : "";
    } else if (field) {
      raw = field.value;
    } else {
      raw = td.textContent;
    }
    raw = raw.trim();
    if (kind === "num") {
      // First number only: "$1,234" -> 1234, and an equity cell's "52% of value"
      // hint underneath doesn't get glued onto the figure. Money puts its sign
      // before the symbol, so "−$91" (U+2212) sorts as -91.
      var m = raw.match(/([-−])?\$?(\d[\d,]*(?:\.\d+)?|\.\d+)/);
      return m ? (m[1] ? -1 : 1) * parseFloat(m[2].replace(/,/g, "")) : MISSING;
    }
    if (raw === "" || raw === "—" || raw === "— none —") return MISSING;
    return raw.toLowerCase();
  }

  function sortKey(table) {
    return "nw.sort." + table.id;
  }

  function sortTable(table, th, dir) {
    var headers = Array.prototype.slice.call(table.tHead.rows[0].cells);
    var index = headers.indexOf(th);
    var kind = th.dataset.sort || "text";
    // A second click flips it. A first click on an amount starts biggest-first,
    // which is the end anyone sorting by value wants to see.
    var asc;
    if (dir) asc = dir === "asc";
    else if (th.dataset.dir) asc = th.dataset.dir !== "asc";
    else asc = kind !== "num";

    headers.forEach(function (h) {
      if (h !== th) {
        delete h.dataset.dir;
        h.classList.remove("sorted");
      }
    });
    th.dataset.dir = asc ? "asc" : "desc";
    th.classList.add("sorted");

    var tbody = table.tBodies[0];
    var rows = bodyRows(table);
    rows.forEach(function (r, i) {
      if (r.dataset.order === undefined) r.dataset.order = i;
    });
    rows.sort(function (a, b) {
      var va = cellValue(a, index, kind);
      var vb = cellValue(b, index, kind);
      // Blanks ("—", no account) go last whichever way you sort, rather than
      // crowding the top of a descending list.
      if (va !== vb) {
        if (va === MISSING) return 1;
        if (vb === MISSING) return -1;
        if (va < vb) return asc ? -1 : 1;
        if (va > vb) return asc ? 1 : -1;
      }
      // Ties keep the server's order (by name), so an account's group reads A–Z.
      return a.dataset.order - b.dataset.order;
    });
    rows.forEach(function (r) {
      var followers = attachedTo(r);
      tbody.appendChild(r);
      followers.forEach(function (f) { tbody.appendChild(f); });
    });

    if (table.id && table.dataset.sortRemember !== undefined) {
      try {
        window.localStorage.setItem(sortKey(table), JSON.stringify({ col: index, dir: th.dataset.dir }));
      } catch (e) {
        // Forgetting the sort on reload is harmless.
      }
    }

    // Re-sorting changes which rows fall on page 1, so go back to it.
    if (table.id && state[table.id]) state[table.id].page = 1;
    if (table.id) apply(table);
  }

  // Tables marked data-sort-remember come back sorted the way you left them — you
  // sort by account to work through a monthly update, and a save reloads the page.
  function restoreSorts() {
    Array.prototype.forEach.call(document.querySelectorAll("table[data-sort-remember][id]"), function (table) {
      var saved;
      try {
        saved = JSON.parse(window.localStorage.getItem(sortKey(table)) || "null");
      } catch (e) {
        return;
      }
      if (!saved || !table.tHead) return;
      var th = table.tHead.rows[0].cells[saved.col];
      if (th && th.dataset.sort !== undefined) sortTable(table, th, saved.dir);
    });
  }

  function init() {
    restoreSorts();
    applyAll();
  }

  // ---- events -----------------------------------------------------------

  document.addEventListener("click", function (e) {
    var th = e.target.closest("th[data-sort]");
    if (th) {
      var table = th.closest("table");
      if (table) sortTable(table, th);
      return;
    }

    var btn = e.target.closest("[data-page]");
    if (!btn || btn.disabled) return;
    var pager = btn.closest(".pager");
    var target = pager && document.getElementById(pager.dataset.pagerFor);
    if (!target) return;
    settings(target).page = parseInt(btn.dataset.page, 10) || 1;
    apply(target);
    var host = target.closest(".table-scroll") || target;
    host.scrollIntoView({ block: "nearest" });
  });

  document.addEventListener("change", function (e) {
    var size = e.target.closest("[data-page-size]");
    if (size) {
      var table = document.getElementById(size.dataset.pageSize);
      if (!table) return;
      var st = settings(table);
      st.size = parseInt(size.value, 10) || DEFAULT_SIZE;
      st.page = 1;
      try {
        window.localStorage.setItem(key(table), String(st.size));
      } catch (err) {
        // Not being able to remember the choice is not worth failing over.
      }
      apply(table);
      return;
    }

    var sel = e.target.closest("[data-filter-table]");
    if (!sel) return;
    var filtered = document.getElementById(sel.dataset.filterTable);
    if (!filtered) return;
    if (state[filtered.id]) state[filtered.id].page = 1;
    apply(filtered);
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
