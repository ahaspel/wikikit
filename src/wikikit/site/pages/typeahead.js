// Shared typeahead: live article-title + Meili-full-text dropdown.
// Used by both home.html (lands on the index page if Enter is pressed
// without a selection) and index.html (runs the in-page full search
// for that case).
//
// Required global helpers — load them BEFORE this script:
//   article-urls.js  →  BritannicaUrls.filenameToUrl(filename)
//   search-api.js    →  searchClient.searchFulltext(q, opts)  (optional;
//                       full-text typeahead is skipped if absent)
//
// Options:
//   inputEl:               <input> element the user types into
//   dropdownEl:            <ul> for typeahead results (usually inside
//                          a `position:relative` container)
//   getArticles():         returns current [{title, filename,
//                          article_type, body_start}, ...]
//   shouldSkip():          optional — return true to hide the dropdown
//                          and bail (e.g., contributor-search mode)
//   onSubmitNoSelection(): optional — called when Enter is pressed
//                          without a highlighted item.  If omitted the
//                          event is left alone so a surrounding <form>
//                          can submit naturally.
//   onInputAlso(q):        optional — fired after each keystroke with
//                          the trimmed query, for callers that want to
//                          also drive a separate results panel
function initTypeahead({
  inputEl,
  dropdownEl,
  shouldSkip = () => false,
  onSubmitNoSelection,
  onInputAlso,
}) {
  let selected = -1;
  let ftToken = 0;
  let ftDebounce = null;

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }


  function makeSnippet(body, q) {
    if (!body) return "";
    const lower = body.toLowerCase();
    const ql = q.toLowerCase();
    const idx = lower.indexOf(ql);
    if (idx < 0) return "";
    const start = Math.max(0, idx - 30);
    const end = Math.min(body.length, idx + ql.length + 50);
    const before = escapeHtml(body.slice(start, idx));
    const match = escapeHtml(body.slice(idx, idx + ql.length));
    const after = escapeHtml(body.slice(idx + ql.length, end));
    return (start > 0 ? "…" : "") + before + `<mark>${match}</mark>` + after + (end < body.length ? "…" : "");
  }

  function navigateTo(item) {
    let url = BritannicaUrls.filenameToUrl(item.filename);
    if (item._kind === "fulltext") {
      const q = encodeURIComponent(item._query || "");
      url += (url.includes("?") ? "&" : "?") + `q=${q}&match=1`;
    }
    location.href = url;
  }

  function renderTypeahead(items) {
    selected = -1;
    if (!items.length) { dropdownEl.style.display = "none"; return; }
    dropdownEl.innerHTML = items.slice(0, 16).map((a, i) => {
      const kindBadge = a._kind === "fulltext"
        ? `<span style="font-size:0.7rem;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em;margin-right:6px;">text</span>`
        : "";
      const sub = a._snippet
        ? `<div style="font-size:0.78rem;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${a._snippet}</div>`
        : (a.body_start
           ? `<div style="font-size:0.78rem;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(a.body_start)}</div>`
           : "");
      return `<li data-idx="${i}" style="padding:6px 10px;cursor:pointer;border-bottom:1px solid var(--border);">
        <div>${kindBadge}${escapeHtml(a.title)}</div>
        ${sub}
      </li>`;
    }).join("");
    dropdownEl.style.display = "block";
    dropdownEl.querySelectorAll("li").forEach(li => {
      li.addEventListener("mousedown", e => {
        e.preventDefault();
        dropdownEl.style.display = "none";
        inputEl.value = "";
        navigateTo(items[+li.dataset.idx]);
      });
    });
  }

  function highlight(idx) {
    dropdownEl.querySelectorAll("li").forEach((li, i) => {
      li.style.background = i === idx ? "#ebe4d6" : "";
    });
  }

  // The dropdown is the top 16 of the SAME ranked list the full-results page
  // shows — one Meilisearch query, one ranking (search-api.js).  No separate
  // title source, so the two lists cannot diverge.
  async function runSearch(q, myToken) {
    if (typeof searchClient === "undefined") return;
    try {
      const hits = await searchClient.rankedSearch(q, { limit: 50 });
      if (myToken !== ftToken) return;
      const items = hits.slice(0, 16).map(h => ({
        title: h.title,
        filename: h.filename,
        body_start: h.body_start,
        _kind: h._titleMatches > 0 ? "title" : "fulltext",
        _snippet: h._titleMatches > 0 ? "" : makeSnippet(h.body || "", q),
        _query: q,
      }));
      renderTypeahead(items);
    } catch (e) { /* leave the previous list up */ }
  }

  inputEl.addEventListener("input", () => {
    const q = inputEl.value.trim();
    if (!q || shouldSkip()) { dropdownEl.style.display = "none"; return; }
    if (ftDebounce) clearTimeout(ftDebounce);
    const myToken = ++ftToken;
    ftDebounce = setTimeout(() => runSearch(q, myToken), 120);
    if (onInputAlso) onInputAlso(q);
  });

  inputEl.addEventListener("keydown", e => {
    const items = dropdownEl.querySelectorAll("li");
    if (e.key === "ArrowDown" && items.length) {
      e.preventDefault();
      selected = Math.min(selected + 1, items.length - 1);
      highlight(selected);
    } else if (e.key === "ArrowUp" && items.length) {
      e.preventDefault();
      selected = Math.max(selected - 1, 0);
      highlight(selected);
    } else if (e.key === "Enter") {
      if (selected >= 0) {
        e.preventDefault();
        items[selected].dispatchEvent(new MouseEvent("mousedown"));
      } else if (onSubmitNoSelection) {
        e.preventDefault();
        dropdownEl.style.display = "none";
        inputEl.blur();
        onSubmitNoSelection();
      }
      // else: let the surrounding <form> submit naturally
    } else if (e.key === "Escape") {
      dropdownEl.style.display = "none";
    }
  });

  inputEl.addEventListener("blur", () => {
    setTimeout(() => { dropdownEl.style.display = "none"; }, 150);
  });

  // iPad/touch fallback: blur isn't always fired when tapping outside
  // a focused input.  Hide the dropdown on any pointer event that
  // lands outside the input/dropdown pair.
  document.addEventListener("pointerdown", e => {
    if (e.target === inputEl) return;
    if (dropdownEl.contains(e.target)) return;
    dropdownEl.style.display = "none";
  });
}

window.initTypeahead = initTypeahead;
