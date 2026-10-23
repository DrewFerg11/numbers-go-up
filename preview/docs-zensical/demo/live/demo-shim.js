/* Injected into every exported demo page (scripts/demo/export.py), before
 * dashboard.js/detail.js load. Makes the shipped UI -- unmodified -- work
 * against static files instead of a live server:
 *
 *   - window.fetch: maps the two endpoints the UI calls
 *     (/api/stats/overview, /api/stats/history) to the matching
 *     api/overview-<range>.json / api/history-<key>-<range>.json file
 *     export.py already wrote next to this script. An unmapped request
 *     fails loudly (logged + rejected) instead of resolving to an empty
 *     payload, so a new endpoint added to the UI shows up as a broken
 *     demo, not a silently blank chart.
 *   - window.setInterval: refuses to schedule dashboard.js's 60s
 *     auto-refresh (it would only ever re-fetch the same frozen JSON).
 *     Nothing else using setInterval is affected.
 *   - clicks on an internal, root-relative link (the metric tiles/rows'
 *     JS-set "/m/<key>?range=..." and detail.html's server-rendered
 *     "/?range=...", plus base.html's static "/docs" topbar link) are
 *     rewritten relative to this export's own root -- the app always
 *     builds these assuming it's served at "/", which isn't true once
 *     the demo lives under /demo/live/ or a branch preview.
 */
(function () {
  "use strict";

  var root = document.currentScript.src.replace(/demo-shim\.js(?:\?.*)?$/, "");

  function jsonFile(url) {
    var overview = url.match(/^\/api\/stats\/overview\?range=([^&]+)$/);
    if (overview) return root + "api/overview-" + overview[1] + ".json";

    var history = url.match(/^\/api\/stats\/history\?metric=([^&]+)&range=([^&]+)$/);
    if (history) {
      var key = decodeURIComponent(history[1]);
      return root + "api/history-" + key + "-" + history[2] + ".json";
    }
    return null;
  }

  var realFetch = window.fetch.bind(window);
  window.fetch = function (input) {
    var url = typeof input === "string" ? input : input.url;
    var file = jsonFile(url);
    if (file === null) {
      console.error("demo shim: no static file mapped for " + url);
      return Promise.reject(new Error("demo: unmapped request " + url));
    }
    return realFetch(file);
  };

  var realSetInterval = window.setInterval.bind(window);
  window.setInterval = function (fn, delay) {
    // A truthy sentinel, not 0 -- dashboard.js never reads this return
    // value today, but 0 is falsy and would silently break any future
    // `if (state.refreshTimer)` guard the same way a real interval ID
    // (always a positive integer) never would.
    if (delay === 60000) return -1;
    return realSetInterval.apply(null, arguments);
  };

  function rewriteInternalHref(href) {
    var parts = href.split("?");
    var query = parts[1] ? "?" + parts[1] : "";
    if (parts[0] === "/") return root + "index.html" + query;
    if (parts[0] === "/docs") {
      // base.html's topbar API link. The demo always lives exactly two
      // directories below the docs site root (demo/live/, or
      // preview/<branch>/demo/live/), so "../../api/" reaches the real,
      // generated API reference page regardless of deployment prefix --
      // the same reasoning the JSON mapping above relies on via `root`.
      return root + "../../api/";
    }
    var m = parts[0].match(/^\/m\/(.+)$/);
    if (m) return root + "m/" + decodeURIComponent(m[1]) + "/index.html" + query;
    return null;
  }

  document.addEventListener("click", function (event) {
    var a = event.target.closest && event.target.closest("a");
    if (!a) return;
    var href = a.getAttribute("href");
    if (!href || href.charAt(0) !== "/") return;
    var target = rewriteInternalHref(href);
    if (target === null) return;
    event.preventDefault();
    window.location.href = target;
  });
})();
