// Gate GoatCounter counting on the first human input (pointer, wheel, key,
// touch).  Scrapers that execute JS on load never fire these, so they are not
// counted; count.js's own on-load count is disabled below (no_onload).
// Deliberately NOT 'scroll': the browser fires it with no human present on
// #fragment auto-scroll and scroll restoration.
window.goatcounter = { no_onload: true };
(function () {
  'use strict';
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
