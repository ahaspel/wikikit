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
  const SPELLED = { bold: true, italic: true, sup: true, sub: true, math: true, paragraph: true };

  // A formula, lifted out whole as the engine lifts it, comes back as a span
  // of its (escaped) LaTeX for the page to typeset with KaTeX, as the viewer
  // does.  The parity gate reads this class.
  const TEX_OPEN = '<span class="tex">', TEX_CLOSE = "</span>";

  // A block that ends its paragraph (the table's third column) leaves this
  // mark, so a newline right after it is a paragraph.
  const END = "\u0001";

  // Templates, innermost first.
  function templates(t, table) {
    for (let i = 0; i < 12; i++) {
      const next = t.replace(/\{\{\s*([^{}|]+?)\s*\|([^{}]*)\}\}/g, (m, name, body) => {
        const key = name.trim().toLowerCase();
        const c = SPELLED[key] ? null : table[key];
        return c ? c[0] + body.trim() + c[1] + (c[2] && c[2].trim() ? END : "") : m;
      });
      if (next === t) break;
      t = next;
    }
    return t;
  }

  // Bold and italic, then anything left unrendered is a leak; then the
  // formulas back, for the page to typeset.
  function inline(p, table, tex, leaks) {
    p = p.replace(/'''([\s\S]*?)'''/g, (m, x) => table.bold[0] + x + table.bold[1])
         .replace(/''([\s\S]*?)''/g, (m, x) => table.italic[0] + x + table.italic[1]);
    const found = p.match(/\{\{|\}\}|\[\[|\]\]|''+/g);
    if (found) leaks.push(...found);
    return p.replace(/\u0002(\d+)\u0003/g, (m, i) => TEX_OPEN + tex[+i] + TEX_CLOSE);
  }

  // A table — a table printed as one, or a formula set out in rows — in the
  // wikitable forms the vocabulary allows (their openings measured, in the
  // table's "table" entry), its cells spanning rows or columns (rowspan=N| /
  // colspan=N|) and holding inline constructs.  Anything else on a table or a
  // cell is a leak.
  const SPAN = /^((?:\s*(?:rowspan|colspan)\s*=\s*"?\d+"?)+)\s*\|(?!\|)([\s\S]*)$/;

  function tableHtml(attrs, body, table, tex, leaks) {
    const open = table.table.open[attrs.trim()];
    if (open === undefined) leaks.push("{|" + attrs);
    const rows = [[]];
    for (const line of body.split("\n")) {
      const l = line.trim();
      if (l === "") continue;
      if (l.startsWith("|-")) {
        if (l.slice(2).trim()) leaks.push(l);
        rows.push([]);
      } else if (l.startsWith("|")) {
        rows[rows.length - 1].push(...l.slice(1).split("||"));
      } else {
        leaks.push(l);
      }
    }
    const trs = rows.filter(r => r.length).map(r => "<tr>" + r.map(cell => {
      const m = cell.match(SPAN);
      const attr = m ? " " + m[1].trim().split(/\s+(?=rowspan|colspan)/)
        .map(a => a.replace(/\s*=\s*"?(\d+)"?/, '="$1"')).join(" ") : "";
      const content = inline(templates(m ? m[2] : cell, table).split(END).join(""), table, tex, leaks);
      return "<td" + attr + ">" + content.trim() + "</td>";
    }).join("") + "</tr>").join("");
    return (open || table.table.open[""]) + trs + table.table.close;
  }

  function render(text, table) {
    let t = String(text).replace(/\r/g, "").trim();
    t = escapeHtml(t);
    // formulas first: their braces are LaTeX's, never a template's
    const tex = [];
    t = t.replace(/&lt;math&gt;([\s\S]*?)&lt;\/math&gt;/g, (m, x) => {
      tex.push(x.trim());
      return "\u0002" + (tex.length - 1) + "\u0003";
    });
    // the vocabulary's HTML
    for (const tag of ["sup", "sub"]) {
      t = t.replace(new RegExp("&lt;" + tag + "&gt;([\\s\\S]*?)&lt;\\/" + tag + "&gt;", "g"),
                    (m, x) => table[tag][0] + x + table[tag][1]);
    }
    // tables, lifted out whole: a block of their own
    const leaks = [];
    const tables = [];
    t = t.replace(/(^|\n)\{\|([^\n]*)\n([\s\S]*?)\n\|\}(?=\n|$)/g, (m, lead, attrs, body) => {
      tables.push(tableHtml(attrs, body, table, tex, leaks));
      return lead + "\n\u0004" + (tables.length - 1) + "\u0004\n\n";
    });
    t = templates(t, table);
    t = t.replace(new RegExp(END + "[ \\t]*\\n", "g"), "\n\n").split(END).join("");
    // paragraphs: a blank line; any other newline is a space
    const paras = t.split(/\n[ \t]*\n\s*/).map(p => p.replace(/\s*\n\s*/g, " "));
    const out = paras.map(p => {
      const lone = p.trim().match(/^\u0004(\d+)\u0004$/);
      return lone ? tables[+lone[1]] : inline(p, table, tex, leaks);
    });
    return { html: out.filter(p => p !== "").join(table.paragraph), leaks: leaks };
  }

  const api = { render: render };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.DeskRender = api;
})(typeof window !== "undefined" ? window : this);
