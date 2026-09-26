/* numbers-go-up metric detail page. */
(function () {
  "use strict";

  var Ngu = window.Ngu;
  var app = document.querySelector(".detail-page");
  var metricKey = app.dataset.metricKey;
  var isStale = app.dataset.stale === "true";
  var staleSinceTs = isStale && app.dataset.staleSince ? Date.parse(app.dataset.staleSince) / 1000 : null;
  var state = { range: app.dataset.initialRange || "1M" };

  var chart = new window.NguChart(document.getElementById("big-chart"));

  function renderChangePill(change, changePct) {
    var el = document.getElementById("detail-change");
    el.textContent = Ngu.formatChange(change, changePct);
    el.className = "detail-change value-" + Ngu.directionOf(change);
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
    document.getElementById("detail-value").textContent = Ngu.formatValue(value);
    renderChangePill(value - open, open === 0 ? null : Math.round(((value - open) / open) * 10000) / 100);

    chart.render(points, {
      direction: Ngu.directionOf(value - open),
      unit: "",
      staleSinceTs: staleSinceTs,
    });
    // A stale series' chart extends its x-domain to "now" (chart.js's
    // synthesized tail point), so the bar strip's domain must match --
    // otherwise every bar maps too far right, worst at the last one,
    // which ends up drawn under the dashed "no data" tail.
    window.renderChangeBars(
      document.getElementById("chart-bars"),
      data.bars,
      Ngu.barsDomain(points, staleSinceTs)
    );
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

  // The initial theme is already applied by an inline <script> in
  // base.html's <head>, synchronously before first paint -- this only
  // needs to handle the toggle.
  Ngu.initThemeToggle(themeToggle, function () {
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
