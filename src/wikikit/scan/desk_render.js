// The proofreading desk's reader view: one strip's wikitext as the edition
// renders it.  It knows NO markup of its own — it applies the table the engine
// measured by running each construct through its own chain
// (wikikit.scan.vocabulary.render_table) — and the parity gate
// (tests/unit/test_scan_desk.py, `python -m wikikit.scan.desk parity`) holds
// its output to the engine's, strip by strip.  Anything the table does not
// cover is a LEAK: reported, never quietly shown as if it rendered.
//
// Browser: window.DeskRender.render(text, table).  Node: require(...).render.
(function (root) {
  "use strict";

  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // Template constructs are `{{name|body}}`; the others are spelled out.
  const SPELLED = { bold: true, italic: true, sup: true, paragraph: true };

  function render(text, table) {
    let t = String(text).replace(/\r/g, "").trim();
    t = escapeHtml(t);
    // <sup> is the one HTML the vocabulary allows
    t = t.replace(/&lt;sup&gt;([\s\S]*?)&lt;\/sup&gt;/g, (m, x) => table.sup[0] + x + table.sup[1]);
    // templates, innermost first; a block that ends its paragraph (the table's
    // third column) leaves a mark, so a newline right after it is a paragraph
    const END = "\u0001";
    for (let i = 0; i < 12; i++) {
      const next = t.replace(/\{\{\s*([^{}|]+?)\s*\|([^{}]*)\}\}/g, (m, name, body) => {
        const key = name.trim().toLowerCase();
        const c = SPELLED[key] ? null : table[key];
        return c ? c[0] + body.trim() + c[1] + (c[2] && c[2].trim() ? END : "") : m;
      });
      if (next === t) break;
      t = next;
    }
    t = t.replace(new RegExp(END + "[ \\t]*\\n", "g"), "\n\n").split(END).join("");
    // paragraphs: a blank line; any other newline is a space
    const paras = t.split(/\n[ \t]*\n\s*/).map(p => p.replace(/\s*\n\s*/g, " "));
    const leaks = [];
    const out = paras.map(p => {
      p = p.replace(/'''([\s\S]*?)'''/g, (m, x) => table.bold[0] + x + table.bold[1])
           .replace(/''([\s\S]*?)''/g, (m, x) => table.italic[0] + x + table.italic[1]);
      const found = p.match(/\{\{|\}\}|\[\[|\]\]|''+/g);
      if (found) leaks.push(...found);
      return p;
    });
    return { html: out.filter(p => p !== "").join(table.paragraph), leaks: leaks };
  }

  const api = { render: render };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.DeskRender = api;
})(typeof window !== "undefined" ? window : this);
