// Small accessibility fixes for Material's own markup, applied on load:
//  - the search panel is role="dialog" with no accessible name;
//  - the header search form is a "search" landmark, and the API reference
//    embeds a second one (Redoc's), so the two need distinct names;
//  - code blocks that scroll horizontally are not keyboard-reachable, so a
//    keyboard-only reader cannot scroll them. Give those a tab stop.
document.addEventListener("DOMContentLoaded", function () {
  var dialog = document.querySelector('[data-md-component="search"][role="dialog"]');
  if (dialog && !dialog.hasAttribute("aria-label")) {
    dialog.setAttribute("aria-label", "Search");
  }
  var landmark = document.querySelector('.md-search__inner[role="search"]');
  if (landmark && !landmark.hasAttribute("aria-label")) {
    landmark.setAttribute("aria-label", "Site search");
  }

  function focusableScrollers() {
    document.querySelectorAll(".md-typeset pre > code").forEach(function (code) {
      var overflows = code.scrollWidth > code.clientWidth;
      if (overflows && !code.hasAttribute("tabindex")) {
        code.setAttribute("tabindex", "0");
      } else if (!overflows && code.getAttribute("tabindex") === "0") {
        code.removeAttribute("tabindex");
      }
    });
  }
  focusableScrollers();
  window.addEventListener("resize", focusableScrollers);
});
