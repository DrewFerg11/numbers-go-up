/* numbers-go-up metric detail page. */
(function () {
  "use strict";

  var THEME_KEY = "ngu.theme";
  var app = document.querySelector(".detail-page");
  var metricKey = app.dataset.metricKey;
  var isStale = app.dataset.stale === "true";
  var staleSinceTs = isStale && app.dataset.updated ? Date.parse(app.dataset.updated) / 1000 : null;
  var state = { range: app.dataset.initialRange || "1M" };

  var chart = new window.NguChart(document.getElementById("big-chart"));

  function formatValue(value) {
    if (value === null || value === undefined) return "—";
    return Number(value).toLocaleString();
  }

  function directionOf(change) {
    if (change > 0) return "up";
    if (change < 0) return "down";
    return "flat";
  }

  function renderChangePill(change, changePct) {
    var el = document.getElementById("detail-change");
    var dir = directionOf(change);
    var arrow = dir === "up" ? "▲" : dir === "down" ? "▼" : "●";
    var sign = change > 0 ? "+" : "";
    var pct =
      changePct === null || changePct === undefined
        ? ""
        : " (" + (changePct > 0 ? "+" : "") + changePct + "%)";
    el.textContent = arrow + " " + sign + formatValue(change) + pct;
    el.className = "detail-change value-" + dir;
  }

  function fetchHistory(range) {
    var url =
      "/api/stats/history?metric=" +
      encodeURIComponent(metricKey) +
      "&range=" +
      encodeURIComponent(range);
    return fetch(url).then(function (response) {
      if (!response.ok) throw new Error("history fetch failed: " + response.status);
      return response.json();
    });
  }

  function render(data) {
    var points = data.points.map(function (p) {
      return [p.ts, p.value];
    });
    if (points.length === 0) return;

    var open = points[0][1];
    var value = points[points.length - 1][1];
    document.getElementById("detail-value").textContent = formatValue(value);
    renderChangePill(value - open, open === 0 ? null : Math.round(((value - open) / open) * 10000) / 100);

    chart.render(points, {
      direction: directionOf(value - open),
      unit: "",
      staleSinceTs: staleSinceTs,
    });
    window.renderChangeBars(document.getElementById("chart-bars"), data.bars);
  }

  function load() {
    fetchHistory(state.range).then(render).catch(function (err) {
      console.error(err);
    });
  }

  function setRange(range) {
    state.range = range;
    document.querySelectorAll(".range-btn").forEach(function (btn) {
      btn.setAttribute("aria-selected", btn.dataset.range === range ? "true" : "false");
    });
    var url = new URL(window.location.href);
    url.searchParams.set("range", range);
    window.history.replaceState({}, "", url);
    load();
  }

  document.querySelectorAll(".range-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      setRange(btn.dataset.range);
    });
  });

  var themeToggle = document.getElementById("theme-toggle");

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
  }

  (function initTheme() {
    var stored = null;
    try {
      stored = localStorage.getItem(THEME_KEY);
    } catch (e) {
      /* ignore */
    }
    applyTheme(stored || "dark");
  })();

  themeToggle.addEventListener("click", function () {
    var current = document.documentElement.getAttribute("data-theme");
    var next = current === "dark" ? "light" : "dark";
    applyTheme(next);
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch (e) {
      /* ignore */
    }
    load();
  });

  // Recorded-changes timestamps are stored/sent as UTC ISO strings;
  // display them in the browser's local time zone.
  document.querySelectorAll(".rc-time").forEach(function (el) {
    var d = new Date(el.textContent);
    if (!isNaN(d.getTime())) {
      el.textContent = d.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
    }
  });

  load();
})();
