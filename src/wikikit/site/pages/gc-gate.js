// Gate GoatCounter counting on the first human input (pointer, wheel, key,
// touch).  Scrapers that execute JS on load never fire these, so they are not
// counted; count.js's own on-load count is disabled below (no_onload).
// Deliberately NOT 'scroll': the browser fires it with no human present on
// #fragment auto-scroll and scroll restoration.
//
// Automation that drives the mouse passes the input gate (its events are
// isTrusted), so headless Chrome's default 800x600 screen is not counted
// either.  2026-09-30: a scraper sent 271 such hits in 3 days, against 5 at
// 800px wide in the month before; real 800-wide tablets are 1280 tall.
window.goatcounter = { no_onload: true };
(function () {
  'use strict';
  if (screen.width === 800 && screen.height === 600) return;
  var events = ['pointerdown', 'pointermove', 'wheel', 'keydown', 'touchstart'];
  var ready = false, seen = false, fired = false;

  function fire() {
    fired = true;
    events.forEach(function (e) { window.removeEventListener(e, interact); });
    window.goatcounter.count();
  }
  function interact() {
    if (fired) return;
    if (ready) fire();
    else seen = true;
  }
  // count.js loads async; its script tag calls this on load.  If the human
  // interacted before the script arrived, count now.
  window.__gcReady = function () {
    ready = true;
    if (seen && !fired) fire();
  };
  events.forEach(function (e) { window.addEventListener(e, interact, { passive: true }); });
})();
