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
    // A stale series' chart extends its x-domain to "now" (chart.js's
    // synthesized tail point), so the bar strip's domain must match --
    // otherwise every bar maps too far right, worst at the last one,
    // which ends up drawn under the dashed "no data" tail.
    var barsEnd = staleSinceTs != null ? Date.now() / 1000 : points[points.length - 1][0];
    window.renderChangeBars(document.getElementById("chart-bars"), data.bars, {
      start: points[0][0],
      end: barsEnd,
    });
  }

  function load() {
    fetchHistory(state.range).then(render).catch(function (err) {
      console.error(err);
    });
  }

  function setRange(range) {
    // A full navigation, not a client-side re-fetch: the stats row
    // (OPEN/HIGH/AVG-DAY/BEST DAY/FIRST SEEN) and the recorded-changes
    // table are server-rendered from the same range-dependent
    // computation the overview endpoint duplicates client-side for its
    // own stats row -- doing that a second time here, in JS, for a
    // page that already round-trips to the server for /api/stats/history
    // on every range change anyway, would be a second place for the
    // open/high/avg-per-day/best-day math to drift from
    // dashboard.metric_detail. A reload keeps the whole page consistent
    // by construction.
    var url = new URL(window.location.href);
    url.searchParams.set("range", range);
    window.location.href = url.toString();
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
