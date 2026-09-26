/* numbers-go-up: helpers shared by the overview (dashboard.js) and detail
 * (detail.js) pages, which were built in separate phases and had drifted
 * into duplicating each other (#130). Loaded from base.html before either
 * page script, exposing one window.Ngu namespace.
 */
window.Ngu = (function () {
  "use strict";

  var THEME_KEY = "ngu.theme";

  function formatValue(value) {
    if (value === null || value === undefined) return "—";
    return Number(value).toLocaleString();
  }

  function directionOf(change) {
    if (change > 0) return "up";
    if (change < 0) return "down";
    return "flat";
  }

  function formatChange(change, changePct) {
    var dir = directionOf(change);
    var arrow = dir === "up" ? "▲" : dir === "down" ? "▼" : "●";
    var sign = change > 0 ? "+" : "";
    var pct =
      changePct === null || changePct === undefined
        ? ""
        : " (" + (changePct > 0 ? "+" : "") + changePct + "%)";
    return arrow + " " + sign + formatValue(change) + pct;
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

  // The bar strip's domain must match the big chart's own x-domain: a
  // stale series extends its line to "now" (chart.js's synthesized tail
  // point), so the bars have to extend there too, or every bar maps too
  // far right -- worst at the last one, which ends up drawn under the
  // dashed "no data" tail instead of at its own timestamp.
  function barsDomain(points, staleSinceTs) {
    var end =
      staleSinceTs != null
        ? Date.now() / 1000
        : points.length
          ? points[points.length - 1][0]
          : 1;
    return { start: points.length ? points[0][0] : 0, end: end };
  }

  // The initial theme is already applied by an inline <script> in
  // base.html's <head>, synchronously before first paint (avoiding a
  // flash of the wrong theme) -- this only ever needs to handle the
  // toggle click, never apply a theme on load.
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
  }

  function initThemeToggle(button, onChange) {
    button.addEventListener("click", function () {
      var current = document.documentElement.getAttribute("data-theme");
      var next = current === "dark" ? "light" : "dark";
      applyTheme(next);
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch (e) {
        /* ignore */
      }
      if (onChange) onChange(next);
    });
  }

  return {
    formatValue: formatValue,
    directionOf: directionOf,
    formatChange: formatChange,
    relativeTime: relativeTime,
    barsDomain: barsDomain,
    initThemeToggle: initThemeToggle,
  };
})();
