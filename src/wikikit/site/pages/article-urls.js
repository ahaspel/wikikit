// Canonical filename ↔ URL helpers, shared across all viewer pages.
//
// Article JSON filenames are TITLE-INDEPENDENT now: `{stable_id}.json`, where the stable_id
// is `{VV}-{PPPP}-{hash6}` — a 6-hex hash of the section slug.  The readable article name is a
// COSMETIC slug in the URL's second segment: ignored on routing, so a renamed article keeps
// its URL and the id alone resolves.  Old `{VV}-{PPPP}-{section-slug}` URLs are rewritten to
// the hash form by the CloudFront forwarder (which recomputes the same hash), so in production
// this module only ever sees hash ids.

(function () {
  // Cosmetic, readable slug from a title.  Purely decorative (routing is the id), so it keeps
  // accented letters; lowercased, runs of non-letter/digit collapsed to a single hyphen.
  function slugify(s) {
    return String(s || "")
      .toLowerCase()
      .replace(/[^\p{L}\p{N}]+/gu, "-")
      .replace(/^-+|-+$/g, "");
  }

  // filename → stable id (filename minus .json).
  function stableIdOf(filename) {
    return String(filename || "").replace(/\.json$/, "");
  }

  function parseFilename(filename) {
    const base = stableIdOf(filename);
    return { stableId: base, base };
  }

  // filename (+ optional title for a cosmetic slug) → clean `/article/{id}[/name]` URL.
  // One form everywhere: the local server speaks production's URL space (tools/serve.py).
  function filenameToUrl(filename, title) {
    const id = stableIdOf(filename);
    return "/article/" + id + (title ? "/" + slugify(title) : "");
  }

  // /article/{id}[/cosmetic] → {dataBase}/{id}.json.  Routes on the id (first path segment
  // after /article/), ignoring the cosmetic slug; old two-segment URLs resolve the same way.
  function pathnameToJsonPath(pathname, dataBase) {
    const m = pathname.match(/^\/article\/([^/?#]+)/);
    if (!m) return null;
    return `${dataBase}/${decodeURIComponent(m[1])}.json`;
  }

  // json path (+ the loaded title) → clean /article/{id}/{cosmetic} for the address bar.
  function jsonPathToCleanUrl(jsonPath, suffix, title) {
    suffix = suffix || "";
    const m = jsonPath.match(/\/([^/]+)\.json$/);
    if (!m) return null;
    return filenameToUrl(m[1] + ".json", title) + suffix;
  }

  window.BritannicaUrls = {
    parseFilename, filenameToUrl, pathnameToJsonPath, jsonPathToCleanUrl, slugify,
  };
})();
