/* numbers-go-up dashboard: no framework, no bundler.
 * Renders DOM nodes via createElement + textContent only -- metric labels,
 * keys, and attrs come from third-party plugin sources and must never be
 * treated as HTML.
 */
(function () {
  "use strict";

  var REFRESH_MS = 60000;
  var THEME_KEY = "ngu.theme";
  var FOLD_KEY_PREFIX = "ngu.fold.";

  var app = document.querySelector(".app");
  var indexStrip = document.getElementById("index-strip");
  var watchlistBody = document.getElementById("watchlist-body");
  var statusLine = document.getElementById("status-line");
  var emptyState = document.getElementById("empty-state");
  var updatedLabel = document.getElementById("updated-label");
  var themeToggle = document.getElementById("theme-toggle");

  var tileTemplate = document.getElementById("tile-template");
  var groupTemplate = document.getElementById("group-template");
  var rowTemplate = document.getElementById("row-template");
  var statusDotTemplate = document.getElementById("status-dot-template");

  var state = {
    range: app.dataset.initialRange || "1M",
    lastFetchedAt: null,
    refreshTimer: null,
  };

  // Same threshold /health/plugins uses (api.DEFAULT_UNHEALTHY_FAILURES),
  // passed through by the server rather than duplicated as a literal here
  // -- one source of truth, so bumping the server constant also moves the
  // dashboard's red dot.
  var UNHEALTHY_FAILURES = parseInt(app.dataset.unhealthyThreshold, 10) || 3;

  // --- Sparkline: a stepped polyline + area fill, hand-written (about 40
  // lines) rather than a charting library -- dozens of chart-library
  // instances for 30x110px sparklines would be heavier than the big chart.
  function renderSparkline(svg, points, direction) {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    if (!points || points.length < 2) return;

    var width = 100;
    var height = Number(svg.getAttribute("height")) || 28;
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);

    var values = points.map(function (p) {
      return p[1];
    });
    var min = Math.min.apply(null, values);
    var max = Math.max.apply(null, values);
    var range = max - min || 1;
    var times = points.map(function (p) {
      return p[0];
    });
    var tMin = times[0];
    var tMax = times[times.length - 1] || tMin + 1;
    var tSpan = tMax - tMin || 1;

    function x(t) {
      return ((t - tMin) / tSpan) * width;
    }
    function y(v) {
      return height - ((v - min) / range) * (height - 2) - 1;
    }

    // Stepped path: hold the previous value until the next change, since
    // store-on-change data has no meaning between two changes.
    var stepped = [];
    for (var i = 0; i < points.length; i++) {
      var px = x(points[i][0]);
      var py = y(points[i][1]);
      if (i > 0) stepped.push([px, stepped[stepped.length - 1][1]]);
      stepped.push([px, py]);
    }

    var linePath = stepped
      .map(function (p, i) {
        return (i === 0 ? "M" : "L") + p[0].toFixed(2) + "," + p[1].toFixed(2);
      })
      .join(" ");
    var areaPath =
      linePath +
      " L" + width + "," + height + " L0," + height + " Z";

    var color = "var(--" + (direction || "flat") + ")";

    var area = document.createElementNS("http://www.w3.org/2000/svg", "path");
    area.setAttribute("d", areaPath);
    area.setAttribute("fill", color);
    area.setAttribute("opacity", "0.15");
    area.setAttribute("stroke", "none");
    svg.appendChild(area);

    var line = document.createElementNS("http://www.w3.org/2000/svg", "path");
    line.setAttribute("d", linePath);
    line.setAttribute("fill", "none");
    line.setAttribute("stroke", color);
    line.setAttribute("stroke-width", "1.5");
    svg.appendChild(line);
  }

  function directionOf(metric) {
    if (metric.change > 0) return "up";
    if (metric.change < 0) return "down";
    return "flat";
  }

  function formatValue(value) {
    if (value === null || value === undefined) return "—";
    return Number(value).toLocaleString();
  }

  function formatChange(metric) {
    var dir = directionOf(metric);
    var arrow = dir === "up" ? "▲" : dir === "down" ? "▼" : "●";
    var sign = metric.change > 0 ? "+" : "";
    var pct =
      metric.change_pct === null || metric.change_pct === undefined
        ? ""
        : " (" + (metric.change_pct > 0 ? "+" : "") + metric.change_pct + "%)";
    return arrow + " " + sign + formatValue(metric.change) + pct;
  }

  function relativeTime(iso) {
    if (!iso) return "—";
    var then = new Date(iso).getTime();
    var diffSeconds = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (diffSeconds < 60) return diffSeconds + "s";
    var minutes = Math.round(diffSeconds / 60);
    if (minutes < 60) return minutes + "m";
    var hours = Math.round(minutes / 60);
    if (hours < 24) return hours + "h";
    return Math.round(hours / 24) + "d";
  }

  function selectedKey() {
    var params = new URLSearchParams(window.location.search);
    return params.get("m");
  }

  function setSelectedKey(key) {
    var url = new URL(window.location.href);
    url.searchParams.set("m", key);
    window.history.replaceState({}, "", url);
    highlightSelection(key);
  }

  function highlightSelection(key) {
    document.querySelectorAll(".tile.selected, .watchlist-row.selected").forEach(
      function (el) {
        el.classList.remove("selected");
      }
    );
    if (!key) return;
    document.querySelectorAll('[data-key="' + CSS.escape(key) + '"]').forEach(
      function (el) {
        el.classList.add("selected");
      }
    );
  }

  function buildTile(metric) {
    var node = tileTemplate.content.firstElementChild.cloneNode(true);
    node.dataset.key = metric.key;
    // No detail page exists yet (it ships in a stacked follow-up PR), so
    // this stays a non-navigating selector rather than a link to a 404.
    node.removeAttribute("href");
    node.querySelector(".tile-plugin").textContent = metric.plugin.toUpperCase();
    node.querySelector(".tile-label").textContent = metric.label || metric.key;
    node.querySelector(".tile-value").textContent = formatValue(metric.value);
    var change = node.querySelector(".tile-change");
    change.textContent = formatChange(metric);
    change.className = "tile-change " + directionOf(metric);
    renderSparkline(node.querySelector(".tile-spark"), metric.spark, directionOf(metric));
    node.addEventListener("click", function (event) {
      event.preventDefault();
      setSelectedKey(metric.key);
    });
    return node;
  }

  function buildRow(metric) {
    var node = rowTemplate.content.firstElementChild.cloneNode(true);
    node.dataset.key = metric.key;
    var label = node.querySelector(".row-label");
    label.textContent = metric.label || metric.key;
    // No detail page exists yet (it ships in a stacked follow-up PR); an
    // href here would send a left-click, middle-click, or "open in new
    // tab" straight to a 404. The row's own click/Enter handlers below
    // already cover selection.
    label.removeAttribute("href");
    node.querySelector(".row-key").textContent = metric.key;
    if (metric.stale) {
      node.querySelector(".stale-chip").hidden = false;
    }
    renderSparkline(
      node.querySelector(".row-spark"),
      metric.spark,
      metric.stale ? "stale" : directionOf(metric)
    );
    node.querySelector(".row-last").textContent = formatValue(metric.value);
    var change = node.querySelector(".row-change");
    change.textContent = formatChange(metric);
    change.className = "col-change row-change value-" + directionOf(metric);
    node.querySelector(".row-highlow").textContent =
      formatValue(metric.high) + " / " + formatValue(metric.low);
    node.querySelector(".row-updated").textContent = relativeTime(metric.updated);

    node.addEventListener("click", function () {
      setSelectedKey(metric.key);
    });
    node.addEventListener("keydown", function (event) {
      if (event.key === "Enter") setSelectedKey(metric.key);
    });
    return node;
  }

  function groupMetrics(metrics) {
    var order = [];
    var byPlugin = {};
    metrics.forEach(function (metric) {
      if (!byPlugin[metric.plugin]) {
        byPlugin[metric.plugin] = [];
        order.push(metric.plugin);
      }
      byPlugin[metric.plugin].push(metric);
    });
    return order.map(function (plugin) {
      return { plugin: plugin, metrics: byPlugin[plugin] };
    });
  }

  function renderWatchlist(metrics) {
    while (watchlistBody.firstChild) watchlistBody.removeChild(watchlistBody.firstChild);

    groupMetrics(metrics).forEach(function (group) {
      var node = groupTemplate.content.firstElementChild.cloneNode(true);
      node.querySelector(".group-name").textContent = group.plugin;
      node.querySelector(".group-count").textContent =
        group.metrics.length + (group.metrics.length === 1 ? " metric" : " metrics");

      var staticMetrics = group.metrics.filter(function (m) {
        return !m.pattern;
      });
      var patternMetrics = group.metrics.filter(function (m) {
        return m.pattern;
      });

      var rows = node.querySelector(".group-rows");
      staticMetrics.forEach(function (metric) {
        rows.appendChild(buildRow(metric));
      });

      var toggle = node.querySelector(".fold-toggle");
      if (patternMetrics.length && staticMetrics.length) {
        // Mixed static + pattern: pattern series fold under a toggle,
        // remembered per browser.
        var foldKey = FOLD_KEY_PREFIX + group.plugin;
        var expanded = false;
        try {
          expanded = localStorage.getItem(foldKey) === "1";
        } catch (e) {
          /* private browsing / blocked storage: default collapsed */
        }
        var patternContainer = document.createElement("div");
        patternContainer.hidden = !expanded;
        patternMetrics.forEach(function (metric) {
          patternContainer.appendChild(buildRow(metric));
        });
        rows.appendChild(patternContainer);

        toggle.hidden = false;
        toggle.textContent =
          (expanded ? "▾ Hide " : "▸ Show ") +
          patternMetrics.length +
          " per-model series";
        toggle.addEventListener("click", function () {
          expanded = !expanded;
          patternContainer.hidden = !expanded;
          toggle.textContent =
            (expanded ? "▾ Hide " : "▸ Show ") +
            patternMetrics.length +
            " per-model series";
          try {
            localStorage.setItem(foldKey, expanded ? "1" : "0");
          } catch (e) {
            /* ignore */
          }
        });
      } else {
        // Pattern-only (or static-only): list directly, no fold.
        patternMetrics.forEach(function (metric) {
          rows.appendChild(buildRow(metric));
        });
      }

      watchlistBody.appendChild(node);
    });
  }

  function renderIndexStrip(pinnedKeys, metricsByKey) {
    while (indexStrip.firstChild) indexStrip.removeChild(indexStrip.firstChild);
    pinnedKeys.forEach(function (key) {
      var metric = metricsByKey[key];
      if (!metric) return;
      indexStrip.appendChild(buildTile(metric));
    });
  }

  // Mirrors api._unhealthy_reason's rule exactly, so the dot and
  // /health/plugins can't disagree: blocked is unhealthy on the first
  // failure; otherwise an in-flight retry doesn't count as one of its own
  // consecutive failures (consecutive_failures's liveness convention
  // counts an unfinished run, which would flag a healthy mid-poll plugin).
  function statusClass(plugin) {
    if (plugin.status === "blocked") return "error";
    var failures = plugin.consecutive_failures;
    if (plugin.status === "polling") failures = Math.max(failures - 1, 0);
    if (failures >= UNHEALTHY_FAILURES) return "error";
    if (failures >= 1) return "warn";
    return plugin.status === "ok" || plugin.status === "polling" ? "ok" : "";
  }

  function renderStatusLine(plugins) {
    while (statusLine.firstChild) statusLine.removeChild(statusLine.firstChild);
    plugins
      .filter(function (p) {
        return p.enabled;
      })
      .forEach(function (plugin) {
        var node = statusDotTemplate.content.firstElementChild.cloneNode(true);
        var cls = statusClass(plugin);
        node.querySelector(".dot").className = "dot " + cls;
        node.querySelector(".dot-name").textContent =
          plugin.name + (plugin.status === "ok" ? " ok" : " " + plugin.status);
        var title = plugin.last_error
          ? "Last error: " + plugin.last_error
          : "Last poll: " + (plugin.last_poll || "never");
        node.setAttribute("title", title);
        statusLine.appendChild(node);
      });
  }

  function updateUpdatedLabel() {
    if (!state.lastFetchedAt) return;
    var seconds = Math.round((Date.now() - state.lastFetchedAt) / 1000);
    var label = seconds < 60 ? "just now" : Math.round(seconds / 60) + "m ago";
    updatedLabel.textContent = "● Updated " + label;
  }

  function render(data) {
    var metricsByKey = {};
    data.metrics.forEach(function (m) {
      metricsByKey[m.key] = m;
    });

    emptyState.hidden = data.metrics.length > 0;
    indexStrip.hidden = data.metrics.length === 0;
    document.getElementById("watchlist").hidden = data.metrics.length === 0;

    renderIndexStrip(data.pinned, metricsByKey);
    renderWatchlist(data.metrics);
    renderStatusLine(data.plugins);

    state.lastFetchedAt = Date.now();
    updateUpdatedLabel();

    var current = selectedKey();
    highlightSelection(current && metricsByKey[current] ? current : data.pinned[0]);
  }

  function fetchOverview() {
    return fetch("/api/stats/overview?range=" + encodeURIComponent(state.range))
      .then(function (response) {
        if (!response.ok) throw new Error("overview fetch failed: " + response.status);
        return response.json();
      })
      .then(render)
      .catch(function (err) {
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
    fetchOverview();
  }

  document.querySelectorAll(".range-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      setRange(btn.dataset.range);
    });
  });

  // --- Theme --------------------------------------------------------

  // The initial theme is already applied by an inline <script> in
  // base.html's <head>, synchronously before first paint (avoiding a
  // flash of the wrong theme) -- this only needs to handle the toggle.
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
  }

  themeToggle.addEventListener("click", function () {
    var current = document.documentElement.getAttribute("data-theme");
    var next = current === "dark" ? "light" : "dark";
    applyTheme(next);
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch (e) {
      /* ignore */
    }
  });

  // --- Refresh --------------------------------------------------------

  function scheduleRefresh() {
    if (state.refreshTimer) clearInterval(state.refreshTimer);
    state.refreshTimer = setInterval(function () {
      if (document.visibilityState === "hidden") return;
      fetchOverview();
    }, REFRESH_MS);
  }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") fetchOverview();
  });

  setInterval(updateUpdatedLabel, 15000);

  fetchOverview();
  scheduleRefresh();
})();
