// Net worth history: stacked asset bars, liability bars, net worth line, and a
// hover breakdown. No dependencies — draws SVG directly so the page stays
// self-contained and works offline.

(function () {
  "use strict";

  var RANGES = [
    { key: "1y", label: "1Y", years: 1 },
    { key: "3y", label: "3Y", years: 3 },
    { key: "5y", label: "5Y", years: 5 },
    { key: "10y", label: "10Y", years: 10 },
    { key: "all", label: "All", years: null },
  ];

  var NW_COLOR = "#a3e635";
  var LIAB_COLOR = "#ef5f68";
  var PLAIN_ASSET = "#2f7fc4";

  function money(v) {
    var a = Math.abs(v);
    if (a >= 1e9) return "$" + (v / 1e9).toFixed(2) + "B";
    if (a >= 1e6) return "$" + (v / 1e6).toFixed(2) + "M";
    if (a >= 1e3) return "$" + Math.round(v / 1e3) + "k";
    return "$" + Math.round(v);
  }

  function exact(v) {
    return v.toLocaleString("en-US", {
      style: "currency", currency: "USD", maximumFractionDigits: 2,
    });
  }

  function monthLabel(month, short) {
    var parts = month.split("-");
    var m = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][parseInt(parts[1], 10) - 1];
    return short ? m + " " + parts[0].slice(2) : m + " " + parts[0];
  }

  function niceStep(range, count) {
    var raw = range / Math.max(1, count);
    var mag = Math.pow(10, Math.floor(Math.log10(raw || 1)));
    var norm = (raw || 1) / mag;
    var step = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10;
    return step * mag;
  }

  function el(name, attrs) {
    var e = document.createElementNS("http://www.w3.org/2000/svg", name);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }

  function NetWorthChart(root) {
    var raw = JSON.parse(document.getElementById(root.dataset.source).textContent);
    var compact = root.dataset.compact === "1";
    var data = raw.slice().sort(function (a, b) { return a.t - b.t; });

    var state = { range: compact ? "all" : (localStorage.getItem("nwRange") || "all") };

    var controls = root.querySelector("[data-role=controls]");
    var canvas = root.querySelector("[data-role=canvas]");
    var caption = root.querySelector("[data-role=caption]");

    var tip = document.createElement("div");
    tip.className = "nw-tip";
    tip.hidden = true;
    root.appendChild(tip);

    function visible() {
      var r = RANGES.find(function (x) { return x.key === state.range; });
      if (!r || !r.years || !data.length) return data;
      var newest = data[data.length - 1].t;
      var cutoff = newest - r.years * 365.25 * 864e5;
      var out = data.filter(function (d) { return d.t >= cutoff; });
      return out.length ? out : data.slice(-1);
    }

    function buildControls() {
      if (!controls || compact) return;
      controls.innerHTML = "";
      var wrap = document.createElement("div");
      wrap.className = "toggle";
      RANGES.forEach(function (r) {
        var b = document.createElement("button");
        b.type = "button";
        b.textContent = r.label;
        if (r.key === state.range) b.className = "on";
        b.addEventListener("click", function () {
          state.range = r.key;
          localStorage.setItem("nwRange", r.key);
          buildControls();
          draw();
        });
        wrap.appendChild(b);
      });
      controls.appendChild(wrap);
    }

    function tipHTML(d) {
      var h = '<div class="nw-tip-head">' + monthLabel(d.month) +
              (d.label ? ' <span class="nw-tip-note">' + d.label + "</span>" : "") + "</div>";

      h += '<div class="nw-tip-row nw-tip-total"><span>Assets</span><b>' +
           exact(d.assets) + "</b></div>";
      if (d.assetParts.length) {
        d.assetParts.forEach(function (p) {
          h += '<div class="nw-tip-row"><span><i style="background:' + p.color +
               '"></i>' + p.name + "</span><b>" + exact(p.value) + "</b></div>";
          p.children.forEach(function (c) {
            h += '<div class="nw-tip-row nw-tip-sub"><span><span class="nw-tip-tree">└─</span>' +
                 '<i style="background:' + p.color + '"></i>' + c.name +
                 "</span><b>" + exact(c.value) + "</b></div>";
          });
        });
      } else {
        h += '<div class="nw-tip-row nw-tip-none"><span>No breakdown recorded</span></div>';
      }

      h += '<div class="nw-tip-row nw-tip-total"><span>Liabilities</span><b>' +
           exact(d.liabilities) + "</b></div>";
      d.liabParts.forEach(function (p) {
        h += '<div class="nw-tip-row"><span><i style="background:' + LIAB_COLOR +
             '"></i>' + p.name + "</span><b>" + exact(p.value) + "</b></div>";
      });

      h += '<div class="nw-tip-row nw-tip-net"><span><i style="background:' + NW_COLOR +
           '"></i>Net Worth</span><b>' + exact(d.nw) + "</b></div>";
      return h;
    }

    function draw() {
      var pts = visible();
      canvas.innerHTML = "";
      tip.hidden = true;

      if (!pts.length) {
        canvas.innerHTML =
          '<p class="muted small" style="margin:0">No snapshots yet — record one to start the history.</p>';
        if (caption) caption.textContent = "";
        return;
      }

      var W = canvas.clientWidth || 720;
      var H = compact ? 170 : 300;
      var padL = compact ? 52 : 68, padR = 14, padT = 12, padB = 28;
      var plotW = W - padL - padR;
      var plotH = H - padT - padB;

      var top = 0;
      pts.forEach(function (d) {
        top = Math.max(top, d.assets, d.liabilities, d.nw);
      });
      var lowest = 0;
      pts.forEach(function (d) { lowest = Math.min(lowest, d.nw); });

      var step = niceStep(top - lowest, compact ? 3 : 5);
      var gTop = Math.ceil(top / step) * step || step;
      var gBot = Math.floor(lowest / step) * step;

      function Y(v) { return padT + (1 - (v - gBot) / (gTop - gBot)) * plotH; }

      // Each month gets a slot holding an asset bar and a liability bar side by side.
      var slot = plotW / pts.length;
      var barW = Math.max(4, Math.min(26, slot * 0.28));
      function slotCenter(i) { return padL + slot * (i + 0.5); }
      function assetX(i) { return slotCenter(i) - barW - 2; }
      function liabX(i) { return slotCenter(i) + 2; }

      var svg = el("svg", {
        viewBox: "0 0 " + W + " " + H, width: "100%", height: H,
        role: "img", "aria-label": "Net worth history",
      });

      for (var g = gBot; g <= gTop + 1e-6; g += step) {
        svg.appendChild(el("line", {
          x1: padL, x2: W - padR, y1: Y(g), y2: Y(g),
          stroke: "var(--line)", "stroke-width": 1,
        }));
        var lab = el("text", {
          x: padL - 8, y: Y(g) + 4, "text-anchor": "end",
          fill: "var(--muted)", "font-size": compact ? 10 : 11,
        });
        lab.textContent = money(g);
        svg.appendChild(lab);
      }

      pts.forEach(function (d, i) {
        // Assets: stacked by bucket when the snapshot recorded a breakdown,
        // otherwise a single solid bar.
        var baseY = Y(0);
        if (d.assetParts.length) {
          var acc = 0;
          d.assetParts.forEach(function (p) {
            var y0 = Y(acc), y1 = Y(acc + p.value);
            svg.appendChild(el("rect", {
              x: assetX(i), y: y1, width: barW, height: Math.max(0.5, y0 - y1),
              fill: p.color,
            }));
            acc += p.value;
          });
        } else if (d.assets) {
          var ay = Y(d.assets);
          svg.appendChild(el("rect", {
            x: assetX(i), y: ay, width: barW, height: Math.max(0.5, baseY - ay),
            fill: PLAIN_ASSET, opacity: 0.55,
          }));
        }

        if (d.liabilities) {
          var accL = 0;
          var parts = d.liabParts.length
            ? d.liabParts
            : [{ value: d.liabilities }];
          parts.forEach(function (p, idx) {
            var y0 = Y(accL), y1 = Y(accL + p.value);
            svg.appendChild(el("rect", {
              x: liabX(i), y: y1, width: barW, height: Math.max(0.5, y0 - y1),
              fill: LIAB_COLOR, opacity: d.liabParts.length ? 1 - idx * 0.18 : 0.55,
            }));
            accL += p.value;
          });
        }
      });

      // Net worth line over the top.
      if (pts.length > 1) {
        svg.appendChild(el("polyline", {
          fill: "none", stroke: NW_COLOR, "stroke-width": 2.5,
          "stroke-linejoin": "round", "stroke-linecap": "round",
          points: pts.map(function (d, i) { return slotCenter(i) + "," + Y(d.nw); }).join(" "),
        }));
      }
      pts.forEach(function (d, i) {
        svg.appendChild(el("circle", {
          cx: slotCenter(i), cy: Y(d.nw), r: compact ? 2.5 : 3.5, fill: NW_COLOR,
        }));
      });

      var ticks = Math.min(pts.length, compact ? 4 : 8);
      var every = Math.ceil(pts.length / ticks);
      pts.forEach(function (d, i) {
        if (i % every !== 0 && i !== pts.length - 1) return;
        var t = el("text", {
          x: slotCenter(i), y: H - 8, "text-anchor": "middle",
          fill: "var(--muted)", "font-size": compact ? 10 : 11,
        });
        t.textContent = monthLabel(d.month, true);
        svg.appendChild(t);
      });

      // Hover: a full-height guide plus the breakdown panel, snapping to the
      // nearest month so sparse yearly data is still easy to hit.
      var guide = el("rect", {
        x: 0, y: padT, width: 1, height: plotH, fill: "var(--text)", opacity: 0.18,
      });
      guide.style.display = "none";
      svg.appendChild(guide);

      var hit = el("rect", {
        x: padL, y: padT, width: plotW, height: plotH, fill: "transparent",
      });
      svg.appendChild(hit);

      function nearest(clientX) {
        var box = svg.getBoundingClientRect();
        var x = (clientX - box.left) * (W / box.width);
        var i = Math.round((x - padL) / slot - 0.5);
        return Math.max(0, Math.min(pts.length - 1, i));
      }

      function showTip(evt) {
        var i = nearest(evt.clientX);
        var d = pts[i];
        guide.setAttribute("x", slotCenter(i));
        guide.style.display = "";
        tip.innerHTML = tipHTML(d);
        tip.hidden = false;

        // Positioned against the viewport, not the card: the breakdown is often
        // taller than the chart itself, and clamping to the card would push it
        // off-screen or squash it.
        var w = tip.offsetWidth, h = tip.offsetHeight;
        var x = evt.clientX + 18;
        if (x + w > window.innerWidth - 8) x = evt.clientX - w - 18;
        if (x < 8) x = 8;
        var y = evt.clientY - h / 2;
        if (y + h > window.innerHeight - 8) y = window.innerHeight - h - 8;
        if (y < 8) y = 8;
        tip.style.left = x + "px";
        tip.style.top = y + "px";
      }

      hit.addEventListener("mousemove", showTip);
      hit.addEventListener("mouseleave", function () {
        tip.hidden = true;
        guide.style.display = "none";
      });

      canvas.appendChild(svg);

      if (caption) {
        var first = pts[0], last = pts[pts.length - 1];
        var delta = last.nw - first.nw;
        var pct = first.nw ? (delta / Math.abs(first.nw)) * 100 : 0;
        var sign = delta >= 0 ? "+" : "−";
        caption.innerHTML =
          pts.length + " month" + (pts.length === 1 ? "" : "s") + " · " +
          monthLabel(first.month) + " → " + monthLabel(last.month) +
          (pts.length > 1
            ? ' · <span class="' + (delta >= 0 ? "pos" : "neg") + '">' + sign +
              money(Math.abs(delta)) + " (" + sign + Math.abs(pct).toFixed(1) + "%)</span>"
            : "");
      }
    }

    buildControls();
    draw();

    var timer;
    window.addEventListener("resize", function () {
      clearTimeout(timer);
      timer = setTimeout(draw, 120);
    });
  }

  document.querySelectorAll("[data-chart=networth]").forEach(NetWorthChart);
})();
