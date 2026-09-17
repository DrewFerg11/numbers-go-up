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

  // points: [[ts, value], ...] ascending by ts. staleSinceTs: draw a grey
  // dashed continuation to "now" after this timestamp (the series' last
  // good poll), or null/undefined for a healthy series.
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

    var width = this.container.clientWidth || 600;
    var height = this.container.clientHeight || 240;

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
      series: [
        {},
        {
          label: opts.unit || "value",
          stroke: color,
          width: 1.5,
          fill: color + "26",
          paths: global.uPlot.paths.stepped({ align: 1 }),
          points: { show: false },
        },
      ],
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
            opts.onCursor({ ts: u.data[0][idx], value: u.data[1][idx] });
          },
        ],
      },
    };

    this.instance = new global.uPlot(uplotOpts, [xs, ys], this.container);

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
  function renderBars(svgContainer, bars) {
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

    var barWidth = width / bars.length;
    var upColor = cssVar("--up");
    var downColor = cssVar("--down");

    bars.forEach(function (bar, i) {
      var barHeight = (Math.abs(bar.change) / maxAbs) * (height / 2 - 2);
      var x = i * barWidth + barWidth * 0.15;
      var w = barWidth * 0.7;
      var y = bar.change >= 0 ? height / 2 - barHeight : height / 2;
      var rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      rect.setAttribute("x", x.toFixed(2));
      rect.setAttribute("y", y.toFixed(2));
      rect.setAttribute("width", w.toFixed(2));
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
