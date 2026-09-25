/* numbers-go-up big chart: a thin wrapper around vendored uPlot.
 * Stepped line + soft area fill, colored by range direction, with a
 * hand-rolled bar strip underneath for the per-bucket change bars (uPlot
 * draws the line; the bars are a second, much simpler canvas-free SVG so
 * we don't need a second full chart instance for a volume-style strip).
 */
(function (global) {
  "use strict";

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function directionColor(direction) {
    if (direction === "up") return cssVar("--up");
    if (direction === "down") return cssVar("--down");
    if (direction === "stale") return cssVar("--stale");
    return cssVar("--flat");
  }

  // A soft area fill under the line. `color + "26"` (hex alpha) only works
  // when the resolved custom property happens to be 6-digit hex --
  // color-mix works for any valid CSS color the token could resolve to.
  function areaFill(color) {
    return "color-mix(in srgb, " + color + " 15%, transparent)";
  }

  function NguChart(container) {
    this.container = container;
    this.instance = null;
  }

  NguChart.prototype.destroy = function () {
    if (this.instance) {
      this.instance.destroy();
      this.instance = null;
    }
  };

  // points: [[ts, value], ...] ascending by ts. staleSinceTs: the series'
  // last good poll -- points at or after it are drawn as a second series,
  // a grey dashed continuation to "now", instead of the real direction
  // color. null/undefined draws a single healthy-colored line throughout.
  NguChart.prototype.render = function (points, opts) {
    opts = opts || {};
    this.destroy();
    if (!points || points.length < 1) return;

    var color = directionColor(opts.direction);
    var xs = points.map(function (p) {
      return p[0];
    });
    var ys = points.map(function (p) {
      return p[1];
    });

    // A stale series' history ends at its last good poll -- there's no
    // stored point at "now" for the dashed tail to extend to, so
    // synthesize one (carrying the last known value forward) whenever
    // the series is stale, per "the line continues ... to 'now'".
    var lastRealTs = xs[xs.length - 1];
    if (opts.staleSinceTs != null) {
      var nowTs = Math.floor(Date.now() / 1000);
      if (xs[xs.length - 1] < nowTs) {
        xs.push(nowTs);
        ys.push(ys[ys.length - 1]);
      }
    }

    var width = this.container.clientWidth || 600;
    var height = this.container.clientHeight || 240;

    var mainSeries = {
      label: opts.unit || "value",
      stroke: color,
      width: 1.5,
      fill: areaFill(color),
      paths: global.uPlot.paths.stepped({ align: 1 }),
      points: { show: false },
    };
    var series = [{}, mainSeries];
    var data = [xs, ys];

    if (opts.staleSinceTs != null) {
      // For a store-on-change series, staleSinceTs (the plugin's last OK
      // poll) can land after the last *stored* point -- there's no real
      // point at or after it for the split search to find besides the
      // synthesized "now" point itself, which would make the whole line
      // main-colored with no tail at all. Clamping the search threshold
      // to the last real point guarantees the split lands there instead,
      // so everything from it onward (through the synthesized point)
      // draws as the dashed tail.
      var splitThreshold = Math.min(opts.staleSinceTs, lastRealTs);
      var splitIdx = xs.findIndex(function (ts) {
        return ts >= splitThreshold;
      });
      if (splitIdx === -1) splitIdx = xs.length - 1;

      // The boundary point is duplicated into both series (not just the
      // stale one) so the healthy line and the dashed tail visually meet
      // instead of leaving a gap.
      var hasStaleTail = splitIdx < xs.length - 1;
      var mainYs = ys.map(function (v, i) {
        return i <= splitIdx ? v : null;
      });
      var staleYs = ys.map(function (v, i) {
        return i >= splitIdx ? v : null;
      });

      // No area fill once the line is split -- a soft fill abruptly
      // truncated at the stale boundary reads as a rendering glitch, not
      // as "this part of the chart is stale".
      if (hasStaleTail) mainSeries.fill = null;
      data = [xs, mainYs, staleYs];
      series = [
        {},
        mainSeries,
        {
          label: "no data since last good poll",
          stroke: cssVar("--stale"),
          width: 1.5,
          dash: [4, 4],
          paths: global.uPlot.paths.stepped({ align: 1 }),
          points: { show: false },
        },
      ];
    }

    var uplotOpts = {
      width: width,
      height: height,
      padding: [10, 10, 0, 0],
      legend: { show: false },
      scales: { x: { time: true } },
      axes: [
        { stroke: cssVar("--text-muted"), grid: { stroke: cssVar("--border") } },
        {
          side: 1,
          stroke: cssVar("--text-muted"),
          grid: { stroke: cssVar("--border") },
        },
      ],
      series: series,
      // Cursor point markers are on by default; explicitly setting
      // `cursor.points.show: true` crashes this uPlot build (a boolean
      // literal there conflicts with its internal default-function path),
      // so leave `cursor` unset and just hook the readout below.
      hooks: {
        setCursor: [
          function (u) {
            if (!opts.onCursor) return;
            var idx = u.cursor.idx;
            if (idx == null) {
              opts.onCursor(null);
              return;
            }
            // With a stale tail, the value lives in whichever of the two
            // series (main / stale) isn't null at this index.
            var value = u.data[1][idx];
            if (value == null && u.data[2] !== undefined) value = u.data[2][idx];
            opts.onCursor({ ts: u.data[0][idx], value: value });
          },
        ],
      },
    };

    this.instance = new global.uPlot(uplotOpts, data, this.container);

    if (!this._resizeObserver) {
      var self = this;
      this._resizeObserver = new ResizeObserver(function () {
        if (self.instance) {
          self.instance.setSize({
            width: self.container.clientWidth || 600,
            height: self.container.clientHeight || 240,
          });
        }
      });
      this._resizeObserver.observe(this.container);
    }
  };

  // Change bars: a small hand-rolled SVG bar strip (not a second uPlot
  // instance -- a volume-style strip is simple enough that a full chart
  // library would be overkill for it).
  //
  // domain: {start, end} unix seconds -- the same time range the big
  // chart's line is drawn across (its points' first/last timestamps).
  // Bars are positioned by `bar.ts` against this domain, not by array
  // index: _bucket_changes omits empty buckets, so under real
  // store-on-change data (gaps between changes are the norm, not the
  // exception) evenly spacing N bars across the strip would put a bar
  // under the wrong point in time relative to the line above it. Falls
  // back to the bars' own first/last ts if no domain is given.
  function renderBars(svgContainer, bars, domain) {
    while (svgContainer.firstChild) svgContainer.removeChild(svgContainer.firstChild);
    if (!bars || bars.length === 0) return;

    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", "100%");
    svg.setAttribute("height", "100%");
    var width = 1000;
    var height = 100;
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    svg.setAttribute("preserveAspectRatio", "none");

    var maxAbs = Math.max.apply(
      null,
      bars.map(function (b) {
        return Math.abs(b.change);
      })
    );
    maxAbs = maxAbs || 1;

    var start = domain ? domain.start : bars[0].ts;
    var end = domain ? domain.end : bars[bars.length - 1].ts;
    var span = Math.max(end - start, 1);
    var barPixelWidth = Math.max((width / bars.length) * 0.6, 2);
    var upColor = cssVar("--up");
    var downColor = cssVar("--down");

    bars.forEach(function (bar) {
      var barHeight = (Math.abs(bar.change) / maxAbs) * (height / 2 - 2);
      var centerX = ((bar.ts - start) / span) * width;
      var x = centerX - barPixelWidth / 2;
      var y = bar.change >= 0 ? height / 2 - barHeight : height / 2;
      var rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      rect.setAttribute("x", x.toFixed(2));
      rect.setAttribute("y", y.toFixed(2));
      rect.setAttribute("width", barPixelWidth.toFixed(2));
      rect.setAttribute("height", Math.max(barHeight, 1).toFixed(2));
      rect.setAttribute("fill", bar.change >= 0 ? upColor : downColor);
      svg.appendChild(rect);
    });

    svgContainer.appendChild(svg);
  }

  global.NguChart = NguChart;
  global.renderChangeBars = renderBars;
  global.nguCssVar = cssVar;
})(window);
