// Click-to-edit fields that save on blur, with an undo toast.

(function () {
  "use strict";

  var toastEl = null;
  var toastTimer = null;

  function toast(msg, undoFn, isError) {
    clearTimeout(toastTimer);
    if (!toastEl) {
      toastEl = document.createElement("div");
      toastEl.className = "toast-live";
      document.body.appendChild(toastEl);
    }
    toastEl.innerHTML = "";
    toastEl.classList.toggle("err", !!isError);

    var span = document.createElement("span");
    span.textContent = msg;
    toastEl.appendChild(span);

    if (undoFn) {
      var b = document.createElement("button");
      b.type = "button";
      b.textContent = "Undo";
      b.addEventListener("click", function () {
        hide();
        undoFn();
      });
      toastEl.appendChild(b);
    }
    toastEl.classList.add("show");
    toastTimer = setTimeout(hide, undoFn ? 9000 : 5000);
  }

  function hide() {
    if (toastEl) toastEl.classList.remove("show");
  }

  function applyPatch(patch) {
    if (!patch) return;
    Object.keys(patch).forEach(function (key) {
      document.querySelectorAll('[data-bind="' + key + '"]').forEach(function (el) {
        el.textContent = patch[key];
        // Drift/gain figures switch sign, so re-colour them from the new text.
        if (el.dataset.signed !== undefined) {
          var neg = /^[−-]|^sell/.test(patch[key].trim());
          el.classList.toggle("neg", neg);
          el.classList.toggle("pos", !neg && patch[key].trim() !== "—");
        }
      });
    });
  }

  function flash(el, cls) {
    el.classList.remove("saved", "failed");
    void el.offsetWidth;
    el.classList.add(cls);
    setTimeout(function () { el.classList.remove(cls); }, 1200);
  }

  function save(el, value, isUndo) {
    var body = new URLSearchParams();
    body.set("field", el.dataset.inlineField);
    body.set("value", value);

    el.classList.add("saving");
    return fetch(el.dataset.inlineUrl, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body.toString(),
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        el.classList.remove("saving");
        if (!res.ok || !res.j.ok) {
          flash(el, "failed");
          el.value = el.dataset.original || "";
          toast((res.j && res.j.error) || "Could not save", null, true);
          return;
        }
        applyPatch(res.j.patch);
        flash(el, "saved");
        el.dataset.original = el.value;

        // A warning (targets no longer summing to 100%) rides along with the usual
        // message rather than replacing it, so the change can still be undone.
        var warn = res.j.warn ? " — " + res.j.warn : "";
        if (isUndo) {
          toast("Change undone" + warn, null, !!warn);
        } else {
          var prev = res.j.prev;
          toast(label(el) + " saved" + warn, function () {
            el.value = prev;
            save(el, prev, true);
          }, !!warn);
        }
      })
      .catch(function () {
        el.classList.remove("saving");
        flash(el, "failed");
        el.value = el.dataset.original || "";
        toast("Could not reach the server — change not saved", null, true);
      });
  }

  function label(el) {
    return el.dataset.inlineLabel || "Value";
  }

  function commit(el) {
    var original = el.dataset.original;
    if (el.value === original) return;
    if (el.type !== "text" && el.tagName === "INPUT" && el.value.trim() === "") {
      // Blank means "no value"; allow it, the server stores NULL.
    }
    save(el, el.value, false);
  }

  document.addEventListener("focusin", function (e) {
    var el = e.target.closest("[data-inline-url]");
    if (el && el.dataset.original === undefined) el.dataset.original = el.value;
    if (el) el.dataset.original = el.dataset.original !== undefined ? el.dataset.original : el.value;
  });

  document.addEventListener(
    "blur",
    function (e) {
      var el = e.target.closest("[data-inline-url]");
      if (el && el.tagName === "INPUT") commit(el);
    },
    true
  );

  document.addEventListener("change", function (e) {
    var el = e.target.closest("[data-inline-url]");
    if (el && el.tagName === "SELECT") commit(el);
  });

  document.addEventListener("keydown", function (e) {
    var el = e.target.closest("[data-inline-url]");
    if (!el) return;
    if (e.key === "Enter" && el.tagName === "INPUT") {
      e.preventDefault();
      el.blur();
    } else if (e.key === "Escape") {
      el.value = el.dataset.original || "";
      el.blur();
    }
  });

  // Seed originals so the first blur can tell whether anything changed.
  document.querySelectorAll("[data-inline-url]").forEach(function (el) {
    el.dataset.original = el.value;
  });
})();
