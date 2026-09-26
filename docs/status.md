# Source status

Does this still work today? A daily canary (`sources-canary.yml`) probes
every source against its real, live endpoint and publishes the result
here -- read from this site's own origin, no CDN and no third-party
requests.

!!! note "Read this before a red row worries you"
    MakerWorld and TikTok run their probes from GitHub Actions' shared
    Azure IP ranges, which both of those platforms occasionally block --
    while working completely normally from a home connection ([#30](https://github.com/DrewFerg11/numbers-go-up/issues/30)).
    A single red row for either of those two is not evidence anything is
    broken; it's labeled **unverified from CI** rather than broken for
    exactly that reason. Everything else failing is a stronger signal.

    The project's own filing policy treats a single failure as noise too:
    only two *consecutive* daily failures open a tracking issue, and a
    recovery gets a comment rather than an auto-close (a 403 that clears
    on its own is exactly the kind of intermittent break an auto-close
    would hide). This page shows the latest run regardless -- a lone red
    row here doesn't necessarily mean an issue has been filed.

<div id="status-meta" style="margin:1em 0;font-size:.9em;color:var(--md-default-fg-color--light)"></div>

<table id="status-table" style="width:100%">
  <thead>
    <tr><th>Source</th><th>Status</th><th>Last checked</th></tr>
  </thead>
  <tbody id="status-body">
    <tr><td colspan="3">Loading&hellip;</td></tr>
  </tbody>
</table>

<script>
(function () {
  "use strict";

  var AZURE_CAVEAT_NOTE =
    "Runs from GitHub Actions' shared IP ranges, which this platform " +
    "sometimes blocks -- see the note above. Unverified, not broken.";

  var STATUS_LABEL = { ok: "OK", broken: "Broken", skipped: "Skipped (no result)" };
  var STATUS_COLOR = { ok: "#2e7d32", broken: "#c62828", skipped: "#888" };

  function row(cells) {
    var tr = document.createElement("tr");
    cells.forEach(function (cell) {
      var td = document.createElement("td");
      if (cell.color) td.style.color = cell.color;
      td.textContent = cell.text;
      if (cell.title) td.title = cell.title;
      tr.appendChild(td);
    });
    return tr;
  }

  function renderMissing(message) {
    document.getElementById("status-meta").textContent = message;
    var body = document.getElementById("status-body");
    body.textContent = "";
    body.appendChild(row([{ text: "—" }, { text: message, color: "#888" }, { text: "—" }]));
  }

  function renderResult(data) {
    var generatedAt = new Date(data.generated_at);
    var ageHours = (Date.now() - generatedAt.getTime()) / 3600000;
    var metaText = "Last canary run: " + data.generated_at;
    if (ageHours > 36) {
      metaText += " (" + Math.round(ageHours / 24) + " day(s) ago -- stale; the daily schedule may be paused)";
    }
    document.getElementById("status-meta").textContent = metaText;

    var body = document.getElementById("status-body");
    body.textContent = "";
    data.sources.forEach(function (source) {
      var label = STATUS_LABEL[source.status] || source.status;
      var isCaveatedBroken = source.azure_ip_caveat && source.status === "broken";
      if (isCaveatedBroken) label = "Unverified from CI";
      body.appendChild(
        row([
          { text: source.plugin },
          {
            text: label,
            color: isCaveatedBroken ? "#b8860b" : STATUS_COLOR[source.status] || "#888",
            title: isCaveatedBroken ? AZURE_CAVEAT_NOTE : undefined,
          },
          { text: source.checked_at },
        ])
      );
    });
  }

  fetch("sources.json")
    .then(function (response) {
      if (!response.ok) throw new Error("no status file (" + response.status + ")");
      return response.json();
    })
    .then(renderResult)
    .catch(function () {
      renderMissing("No canary result yet.");
    });
})();
</script>
