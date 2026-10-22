// Row action menus ("⋯") and collapsible sections.

(function () {
  "use strict";

  // Menus are positioned with `fixed` rather than `absolute` so they escape the
  // horizontal scroll container the tables live in, which would otherwise clip them.
  function closeAllMenus() {
    document.querySelectorAll(".menu-list").forEach(function (m) {
      m.hidden = true;
      var btn = m.previousElementSibling;
      if (btn) btn.setAttribute("aria-expanded", "false");
    });
  }

  function placeMenu(btn, list) {
    list.hidden = false;
    var b = btn.getBoundingClientRect();
    var w = list.offsetWidth;
    var h = list.offsetHeight;

    var left = b.right - w;
    if (left < 8) left = 8;
    if (left + w > window.innerWidth - 8) left = window.innerWidth - w - 8;

    var top = b.bottom + 4;
    if (top + h > window.innerHeight - 8) {
      var above = b.top - h - 4;
      top = above > 8 ? above : Math.max(8, window.innerHeight - h - 8);
    }

    list.style.left = Math.round(left) + "px";
    list.style.top = Math.round(top) + "px";
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest(".menu-btn");
    if (btn) {
      e.stopPropagation();
      var list = btn.nextElementSibling;
      var wasOpen = !list.hidden;
      closeAllMenus();
      if (!wasOpen) {
        placeMenu(btn, list);
        btn.setAttribute("aria-expanded", "true");
      }
      return;
    }
    if (!e.target.closest(".menu-list")) closeAllMenus();
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeAllMenus();
  });

  window.addEventListener("resize", closeAllMenus);
  window.addEventListener("scroll", closeAllMenus, true);

  // Collapsible panels: [data-toggle="id"] shows/hides #id.
  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-toggle]");
    if (!t) return;
    var panel = document.getElementById(t.dataset.toggle);
    if (!panel) return;
    var show = panel.hidden;
    panel.hidden = !show;
    t.setAttribute("aria-expanded", show ? "true" : "false");
    if (t.dataset.labelOpen && t.dataset.labelClosed) {
      t.textContent = show ? t.dataset.labelOpen : t.dataset.labelClosed;
    }
    // Buttons sharing a group open one panel at a time, so a row of them doesn't
    // stack several long forms on top of each other.
    if (show && t.dataset.toggleGroup) {
      var peers = document.querySelectorAll(
        '[data-toggle-group="' + t.dataset.toggleGroup + '"]'
      );
      Array.prototype.forEach.call(peers, function (other) {
        if (other === t) return;
        var p = document.getElementById(other.dataset.toggle);
        if (!p || p.hidden) return;
        p.hidden = true;
        other.setAttribute("aria-expanded", "false");
        if (other.dataset.labelOpen && other.dataset.labelClosed) {
          other.textContent = other.dataset.labelClosed;
        }
      });
    }
    if (show) {
      var first = panel.querySelector("input, select");
      if (first) first.focus();
    }
  });

  // A lot ledger lives in a <tr> directly under its holding, so showing the panel
  // means showing that row too — otherwise the panel unhides inside a row that is
  // still display:none and nothing appears.
  function setPanel(panel, show) {
    panel.hidden = !show;
    var row = panel.closest("tr.ledger-row");
    if (row) row.hidden = !show;
  }

  // Panels opened from a row menu (edit forms).
  window.openPanel = function (id) {
    closeAllMenus();
    document.querySelectorAll(".edit-panel").forEach(function (p) {
      if (p.id !== id) setPanel(p, false);
    });
    var panel = document.getElementById(id);
    if (!panel) return;
    setPanel(panel, true);
    panel.scrollIntoView({ block: "nearest", behavior: "smooth" });
    var first = panel.querySelector("input, select");
    if (first) first.focus();
  };

  window.closePanels = function () {
    document.querySelectorAll(".edit-panel").forEach(function (p) {
      setPanel(p, false);
    });
    document.querySelectorAll("[data-panel-toggle]").forEach(function (b) {
      b.setAttribute("aria-expanded", "false");
    });
  };

  // Click again to collapse, for panels opened from a visible button.
  window.togglePanel = function (id, btn) {
    var panel = document.getElementById(id);
    if (!panel) return;
    var wasOpen = !panel.hidden;
    closeAllMenus();
    window.closePanels();
    if (!wasOpen) {
      setPanel(panel, true);
      if (btn) btn.setAttribute("aria-expanded", "true");
      panel.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  };
})();

// The lot form's funding field means the opposite thing on a sell: a buy debits the
// account, a sell credits it. Relabel, and clear the choice when the side flips so a
// credit can't silently land on the account picked as the debit source.
(function () {
  "use strict";

  function syncFunding(form) {
    var side = form.querySelector('select[name="side"]');
    var select = form.querySelector(".funding-select");
    if (!side || !select) return;
    var selling = side.value === "sell";

    var label = form.querySelector(".funding-label");
    if (label) label.textContent = selling ? "Deposit to" : "Paid from";

    var ext = form.querySelector(".funding-external");
    if (ext) {
      ext.textContent = selling
        ? "— external (proceeds not tracked) —"
        : "— external funds (don't move cash) —";
    }
    var buyHelp = form.querySelector(".funding-help");
    var sellHelp = form.querySelector(".funding-help-sell");
    if (buyHelp) buyHelp.hidden = selling;
    if (sellHelp) sellHelp.hidden = !selling;
  }

  document.addEventListener("change", function (e) {
    var side = e.target.closest('select[name="side"]');
    if (!side) return;
    var form = side.closest("form");
    if (!form) return;
    var select = form.querySelector(".funding-select");
    // The meaning inverted, so force a fresh choice.
    if (select) select.selectedIndex = 0;
    syncFunding(form);
  });

  document.querySelectorAll('form select[name="side"]').forEach(function (s) {
    syncFunding(s.closest("form"));
  });
})();
