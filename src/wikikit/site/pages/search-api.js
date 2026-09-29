// Shared Meilisearch client for full-text article search.
//
// Both index.html's main search typeahead and viewer.html's
// in-article typeahead call the same code path here.  Prior to
// extraction each page had its own inline ``runFulltext`` — duplicate
// logic with identical intent, which drifted: the plate-type
// exclusion fix landed in one file then had to be re-discovered and
// re-applied in the other.  Keeping the Meilisearch request body
// + post-filter in one place prevents recurrence of that class of
// drift.
//
// Usage:
//   const client = createSearchClient(MEILI_URL, MEILI_KEY);
//   const hits = await client.searchFulltext(q, {
//     limit: 8,
//     excludeFilenames: new Set(["alreadyShown.json", …]),
//     excludePlates: true,     // default
//   });
//
// Each hit is ``{title, filename, body}`` already post-filtered for
// substring match (case-insensitive) and caller-excluded filenames.
// Callers do their own rendering.

(function () {
  // ── Shared search matching + ranking ──────────────────────────────────
  // ONE fold + ONE title-rank used by BOTH the dropdown (typeahead.js) and the
  // full-results list (index.html), so they can never drift into two orderings.
  // Meilisearch itself accent-folds, so it RETURNS "ZÜRICH" for "zurich"; the
  // client must fold identically or it scores those hits 0 and buries/drops them.

  // Lowercased, diacritics stripped: "ZÜRICH" -> "zurich", so an un-accented
  // query matches an accented title/body.  Double quotes are DELIMITERS, not
  // content — `"battle of hastings"` is the documented exact-order syntax, and
  // Meilisearch reads it as a phrase, but this fold feeds the client-side
  // substring gate/ranking, where the literal `"` characters matched nothing
  // and every phrase-quoted query died at zero results.  Straight and curly
  // both fold away (bodies print curly).
  function fold(s) {
    return String(s || "").toLowerCase().normalize("NFD")
      .replace(/[̀-ͯ]/g, "").replace(/["“”]/g, "");
  }

  // Match-quality tier for a title against a query (lower = better):
  //   0 exact title · 1 first word · 2 any word · 3 prefix · 4 substring in
  //   title · 5 not in title (body-only hit).  This is the whole ordering
  //   spec: exact title, then title-phrase/word, then partials, then text.
  function titleRank(title, q) {
    const t = fold(title), qf = fold(q);
    if (!qf) return 6;
    if (t === qf) return 0;
    const words = t.split(/[\s,.'’()\-]+/).filter(Boolean);
    if (words[0] === qf) return 1;
    if (words.includes(qf)) return 2;
    if (t.startsWith(qf)) return 3;
    if (t.includes(qf)) return 4;
    return 5;
  }

  // Count of folded, case-insensitive occurrences of q in text.
  function ftCountMatches(text, q) {
    const tf = fold(text), qf = fold(q);
    if (!tf || !qf) return 0;
    let n = 0, i = 0;
    while ((i = tf.indexOf(qf, i)) !== -1) { n++; i += qf.length; }
    return n;
  }

  // Rank a set of Meilisearch hits into THE order both views show: title-match
  // tier (exact→first-word→word→prefix→substring→body-only), then more body
  // matches, then alpha.  Pure function of the hits, so the dropdown and the page
  // — which slice the same list — cannot differ.
  function rankHits(hits, q) {
    for (const h of (hits || [])) {
      h._titleRank = titleRank(h.title, q);
      h._titleMatches = ftCountMatches(h.title, q);
      h._bodyMatches = ftCountMatches(h.body, q);
      h._matchCount = h._titleMatches + h._bodyMatches;
    }
    (hits || []).sort((a, b) =>
      (a._titleRank - b._titleRank)
      || ((b._bodyMatches || 0) - (a._bodyMatches || 0))
      || (a.title || "").localeCompare(b.title || ""));
    return hits || [];
  }

  window.BritannicaSearch = { fold, titleRank, ftCountMatches, rankHits };

  function createSearchClient(meiliUrl, meiliKey) {
    async function searchFulltext(q, options) {
        options = options || {};
        // ONE default limit so the dropdown and the page fetch the SAME hit set
        // (the dropdown just renders fewer rows).
        const limit = options.limit || 50;
        const excludeFilenames = options.excludeFilenames || new Set();
        const excludePlates = options.excludePlates !== false;  // default true
        // by:Name — a contributor restriction.  The server has no substring
        // filter operator, so the restriction is a client post-filter on the
        // hit's own contributors field (retrieved below).  A BARE by:Name (no
        // query text) instead SEARCHES the contributors attribute — an empty-q
        // placeholder search would return arbitrary documents and the
        // post-filter would sieve a meaningless page.
        const by = options.by || null;
        const body = {
          q: q || by || "",
          limit: limit,
          matchingStrategy: "all",
          // Full display set so BOTH views render straight from the hit — no
          // second source needed.
          attributesToRetrieve: ["id", "title", "filename", "body", "body_start",
            "volume", "page_start", "page_end", "article_type", "contributors"],
        };
        // Plates inherit their parent article's title, so without this filter
        // searching "SHIPBUILDING" returns the main article plus its plate
        // duplicates.  Any caller filter (vol:N etc.) is AND-ed on.
        const clauses = [];
        if (excludePlates) clauses.push('NOT article_type = "plate"');
        if (options.filter) clauses.push(options.filter);
        if (clauses.length === 1) body.filter = clauses[0];
        else if (clauses.length > 1) body.filter = clauses;
        if (by && !q) body.attributesToSearchOn = ["contributors"];
        const resp = await fetch(
          `${meiliUrl}/indexes/articles/search`,
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Authorization": `Bearer ${meiliKey}`,
            },
            body: JSON.stringify(body),
          });
        const data = await resp.json();
        return (data.hits || [])
          // The substring gate only applies to a TEXT query — a bare by:Name
          // has no q, and its match lives in the contributors field.
          .filter(h => !q
            || ftCountMatches(h.body, q) > 0 || ftCountMatches(h.title, q) > 0)
          .filter(h => !by || fold(h.contributors || "").includes(fold(by)))
          .filter(h => !excludeFilenames.has(h.filename));
    }

    // THE one entry point both views call.  The dropdown renders
    // `.slice(0, N)` of exactly this list; the page renders all of it — so
    // they are the same results in the same order, by construction.
    async function rankedSearch(q, options) {
      return rankHits(await searchFulltext(q, options), q);
    }

    return { searchFulltext, rankedSearch };
  }
  window.createSearchClient = createSearchClient;
})();
