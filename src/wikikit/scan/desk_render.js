// The proofreading desk's reader view: one strip's wikitext as the edition
// renders it.  It knows NO markup of its own — it applies the table the engine
// measured by running each construct through its own chain
// (wikikit.scan.vocabulary.render_table) — and the parity gate
// (tests/unit/test_scan_desk.py, `python -m wikikit.scan.desk parity`) holds
// its output to the engine's, strip by strip.  Anything the table does not
// cover is a LEAK: reported, never quietly shown as if it rendered.
//
// A whole page also holds what assembly writes into it (its figures, a figure
// with type beside it, the mark that it opens a paragraph), measured as the
// rest is: the table's "figure", "inset" and "nop".  A figure's image is the
// desk's own copy, `files` mapping its file name to it.
//
// Browser: window.DeskRender.render(text, table, files).  Node: require(...).render.
(function (root) {
  "use strict";

  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // Template constructs are `{{name|body}}`; the others are spelled out.
  const SPELLED = { bold: true, italic: true, sup: true, sub: true, math: true, paragraph: true,
                    poem: true, table: true, figure: true, inset: true, nop: true, resumes: true,
                    gap: true, quotes: true };

  // A formula, lifted out whole as the engine lifts it, comes back as a span
  // of its (escaped) LaTeX for the page to typeset with KaTeX, as the viewer
  // does.  The parity gate reads this class.
  const TEX_OPEN = '<span class="tex">', TEX_CLOSE = "</span>";

  // A block that ends its paragraph (the table's third column) leaves this
  // mark, so a newline right after it is a paragraph — unless what follows
  // reads as the rest of a sentence, or is no text but another construct
  // (the engine's test, the table's "resumes").
  const END = "\u0001";
  // Every construct opens with this mark, so the text after a block can tell
  // words (which may open a paragraph) from another construct (which does not).
  const ELEMENT = "\u0007";
  const NOT_TEXT = /^[\u0002\u0004\u0005\u0006\u0007]/;

  // A block's END and the newline after it -> a paragraph or a space.
  function afterBlocks(t, table) {
    const r = table.resumes;
    const joiner = r ? new RegExp("^(?:" + r.joiner + ")", "i") : null;
    const continues = s => !!joiner && (joiner.test(s) || /^\p{Ll}/u.test(s));
    if (r) {
      // a sentence a block cut at a , ; or : resumes across a blank line
      const open = "[" + r.open.replace(/[\]\\^-]/g, "\\$&") + "]";
      t = t.replace(new RegExp("(" + open + ")((?:</\\w+>|" + END + ")+)[ \\t]*\\n(?:[ \\t]*\\n)+\\s*", "g"),
        (m, p, closes, off, all) =>
          /<\/div>/.test(closes) && continues(all.slice(off + m.length)) ? p + closes + " " : m);
    }
    t = t.replace(new RegExp(END + "[ \\t]*\\n(?![ \\t]*\\n)\\s*", "g"), (m, off, all) => {
      if (!r) return "\n\n";
      const rest = all.slice(off + m.length);
      return !rest || NOT_TEXT.test(rest) || continues(rest) ? " " : "\n\n";
    });
    return t.replace(new RegExp(END + "[ \\t]*\\n", "g"), "\n\n").split(END).join("");
  }

  // A template of arguments, as measured (vocabulary._measure_args): the
  // parts around and between the arguments the engine shows, in its order.
  function withArgs(body, c) {
    const args = body.split("|").map(a => a.trim());
    let out = c.parts[0];
    c.order.forEach((i, k) => { out += (args[i] || "") + c.parts[k + 1]; });
    return out + (c.after && c.after.trim() ? END : "");
  }

  // Templates, innermost first.
  function templates(t, table) {
    for (let i = 0; i < 12; i++) {
      const next = t.replace(/\{\{\s*([^{}|]+?)\s*\|([^{}]*)\}\}/g, (m, name, body) => {
        const key = name.trim().toLowerCase();
        const c = SPELLED[key] ? null : table[key];
        if (c && c.parts) return ELEMENT + withArgs(body, c);
        return c ? ELEMENT + c[0] + body.trim() + c[1] + (c[2] && c[2].trim() ? END : "") : m;
      });
      if (next === t) break;
      t = next;
    }
    return t;
  }

  // Bold and italic, then anything left unrendered is a leak; then the
  // formulas back, for the page to typeset.
  function inline(p, table, tex, leaks) {
    p = p.replace(/'''''([^']+)'''''/g, (m, x) => table.italic[0] + table.bold[0] + x + table.bold[1] + table.italic[1])
         .replace(/'''([\s\S]*?)'''/g, (m, x) => table.bold[0] + x + table.bold[1])
         .replace(/''([\s\S]*?)''/g, (m, x) => table.italic[0] + x + table.italic[1]);
    const found = p.match(/\{\{|\}\}|\[\[|\]\]|''+/g);
    if (found) leaks.push(...found);
    return p.replace(/\u0002(\d+)\u0003/g, (m, i) => TEX_OPEN + tex[+i] + TEX_CLOSE);
  }

  // A table — a table printed as one, or a formula set out in rows — in the
  // wikitable forms the vocabulary allows (their openings measured, in the
  // table's "table" entry), its cells spanning rows or columns (rowspan=N| /
  // colspan=N|) and holding inline constructs.  Anything else on a table or a
  // cell is a leak.  A row or cell may also carry the print's rules and
  // alignment as {{Ts|codes}}, a heading row begins with ! (cells divided by
  // !!), and a cell may hold a brace over rows, {{brace2|N|l/r}} — each
  // rendered from what the engine was measured to make of it (the table's
  // "ts", "th" and "brace" entries), nothing else.
  const SPAN_ATTR = /^\s*(rowspan|colspan)\s*=\s*"?(\d+)"?/;
  const TS_ATTR = /^\s*\{\{Ts\|([^{}]*)\}\}/;

  // The style a run of Ts codes makes, as measured; an unmeasured code leaks.
  function tsStyle(codes, table, leaks) {
    return codes.split("|").map(c => c.trim()).filter(c => c).map(c => {
      const css = (table.table.ts || {})[c];
      if (css === undefined) leaks.push("{{Ts|" + c + "}}");
      return css;
    }).filter(c => c).join(";");
  }

  // A cell: its spans and codes, then a single | and its content — or all content.
  function cellHtml(cell, tag, table, tex, leaks) {
    let rest = cell, spans = [], styles = [], m;
    while ((m = rest.match(SPAN_ATTR) || rest.match(TS_ATTR))) {
      if (m[0].trim().startsWith("{{")) styles.push(tsStyle(m[1], table, leaks));
      else spans.push(m[1] + '="' + m[2] + '"');
      rest = rest.slice(m[0].length);
    }
    const sep = rest.match(/^\s*\|(?!\|)/);
    if ((spans.length || styles.length) && sep) rest = rest.slice(sep[0].length);
    else { rest = cell; spans = []; styles = []; }
    const style = styles.filter(s => s).join(";");
    const attr = (spans.length ? " " + spans.join(" ") : "") + (style ? ' style="' + style + '"' : "");
    const braced = rest.replace(/\{\{\s*brace2\s*\|\s*(\d+)\s*\|\s*([lr])\s*\}\}/g, (b, n, d) => {
      const html = (table.table.brace || {})[n + d];
      if (html === undefined) { leaks.push(b); return ""; }
      return html;
    });
    const content = inline(templates(braced, table).split(END).join(""), table, tex, leaks);
    const [open, close] = tag === "th" ? (table.table.th || ["<th>", "</th>"]) : ["<td>", "</td>"];
    return open.replace(/>$/, attr + ">") + content.trim() + close;
  }

  function tableHtml(attrs, body, table, tex, leaks) {
    const open = table.table.open[attrs.trim()];
    if (open === undefined) leaks.push("{|" + attrs);
    const rows = [{ style: "", cells: [] }];
    for (const line of body.split("\n")) {
      const l = line.trim();
      if (l === "") continue;
      if (l.startsWith("|-")) {
        let rest = l.slice(2), styles = [], m;
        while ((m = rest.match(TS_ATTR))) { styles.push(tsStyle(m[1], table, leaks)); rest = rest.slice(m[0].length); }
        if (rest.trim()) leaks.push(l);
        rows.push({ style: styles.filter(s => s).join(";"), cells: [] });
      } else if (l.startsWith("!")) {
        rows[rows.length - 1].cells.push(...l.slice(1).split("!!").map(c => ["th", c]));
      } else if (l.startsWith("|")) {
        rows[rows.length - 1].cells.push(...l.slice(1).split("||").map(c => ["td", c]));
      } else {
        leaks.push(l);
      }
    }
    const trs = rows.filter(r => r.cells.length).map(r =>
      (r.style ? '<tr style="' + r.style + '">' : "<tr>") +
      r.cells.map(([tag, cell]) => cellHtml(cell, tag, table, tex, leaks)).join("") + "</tr>").join("");
    return (open || table.table.open[""]) + trs + table.table.close;
  }

  // A block of lines (<poem>): each line rendered as running text is, joined
  // by what the engine was measured to put between two lines and between two
  // stanzas (the table's "poem" entry).
  function linesHtml(body, table, tex, leaks) {
    const p = table.poem;
    return p.open + body.replace(/^\n+|\n+$/g, "").split(/\n[ \t]*\n\s*/).map(stanza =>
      stanza.split("\n").map(line =>
        inline(templates(line, table).split(END).join(""), table, tex, leaks).trim()
      ).join(p.line)
    ).join(p.stanza) + p.close;
  }

  // An image as the engine was measured to render it, the desk's copy in it.
  const SLOT = { src: "\u0010src\u0010", file: "\u0010file\u0010", width: "\u0010width\u0010",
                 cap: "\u0010cap\u0010" };
  function imageHtml(html, file, width, cap, files) {
    const name = file.replace(/&amp;/g, "&").trim();
    return html.split(SLOT.src).join(escapeHtml((files || {})[name] || ""))
      .split(SLOT.file).join(file.trim()).split(SLOT.width).join(width)
      .split(SLOT.cap).join(cap);
  }

  // Bold and italic are read line by line, as the edition reads them
  // (MediaWiki's apostrophe runs): a run that crosses another, is left open,
  // or is four or six-plus apostrophes renders there otherwise than it would
  // here — its line is reported as a leak, never shown as if it rendered.
  function quoteFaults(text) {
    const faults = [];
    for (const line of text.split("\n")) {
      const stack = [];
      let bad = false;
      for (const m of line.replace(/<math>[\s\S]*?<\/math>/g, "").matchAll(/'{2,}/g)) {
        const kinds = { 2: ["i"], 3: ["b"], 5: ["b", "i"] }[m[0].length];
        if (!kinds) { bad = true; break; }
        const order = kinds.slice().sort((a, b) => stack.lastIndexOf(b) - stack.lastIndexOf(a));
        for (const k of order) {
          const at = stack.lastIndexOf(k);
          if (at < 0) stack.push(k);
          else if (at === stack.length - 1) stack.pop();
          else bad = true;
        }
        if (bad) break;
      }
      if (bad || stack.length) faults.push(line.trim());
    }
    return faults;
  }

  // Straight quotation marks set curly, as the engine sets them for a book
  // that prints them so (preprocess.curly_quotes; the table's "quotes": its
  // own patterns).  Markup is masked first, one character for one, so every
  // offset holds; a line with no pair is left as it was.
  function curlyQuotes(s, q) {
    const re = (p, f) => new RegExp(p, f || "gu");
    const masked = s.replace(re(q.mask, "gsiu"), m => "\0".repeat(m.length));
    const attr = re(q.table_attr, "u");
    let out = "", i = 0;
    for (const line of masked.matchAll(/[^\n]+/g)) {
      const l = line[0];
      if (!l.includes('"') || attr.test(l)) continue;
      const work = l.replace(re(q.pair), (m, x) => "“" + x + "”")
                    .replace(re(q.half_open), (m, x) => "“" + x)
                    .replace(re(q.half_close), (m, x) => x + "”");
      if (work === l) continue;
      const a = line.index, b = a + l.length, text = s.slice(a, b);
      out += s.slice(i, a);
      for (let k = 0; k < work.length; k++) out += work[k] === "\0" ? text[k] : work[k];
      i = b;
    }
    return out + s.slice(i);
  }

  function render(text, table, files) {
    let t = String(text).replace(/\r/g, "").trim();
    if (table.quotes && table.quotes.on) t = curlyQuotes(t, table.quotes);
    const faults = quoteFaults(t);
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
    const leaks = [...faults];
    // what assembly writes into a page: lifted out whole, put back last
    const images = [];
    const keep = h => { images.push(h); return "\u0006" + (images.length - 1) + "\u0006"; };
    if (table.figure) {
      if (table.br !== undefined) t = t.replace(/&lt;br\s*\/?&gt;/g, table.br);
      t = t.replace(/\[\[File:([^|\]\n]+)\|center\|(\d+)px\]\]/g,
                    (m, file, width) => keep(imageHtml(table.figure, file, width, "", files)));
      t = t.replace(/\{\{img float\|file=([^|{}\n]+)\|width=(\d+)px\|align=(left|right)\|cap=((?:[^{}]|\{\{[^{}]*\}\})*)\}\}/g,
                    (m, file, width, align, cap) => keep(imageHtml(table.inset[align], file, width,
                      inline(templates(cap, table).split(END).join(""), table, tex, leaks).trim(), files)));
      t = t.split("{{nop}}").join(ELEMENT + table.nop);
    }
    // an indent, or a space of fixed width: N ems (the table's "gap"), N
    // rounded as the engine rounds it (half to even), at least one
    if (table.gap) {
      t = t.replace(/\{\{\s*(gap|em)\s*(?:\|\s*([\d.]*)[^{}]*)?\}\}/gi, (m, name, n) => {
        const w = parseFloat(n);
        let k = isNaN(w) ? table.gap[name.toLowerCase()] : Math.round(w);
        if (!isNaN(w) && Math.abs(w % 1) === 0.5 && k % 2) k -= 1;
        return ELEMENT + table.gap.unit.repeat(Math.max(1, k));
      });
    }
    // blocks of lines, lifted out whole: their newlines are lines, not spaces
    // (a table measured before the engine kept lines has none: shown as typed)
    const poems = [];
    if (table.poem) {
      t = t.replace(/&lt;poem&gt;([\s\S]*?)&lt;\/poem&gt;/g, (m, body) => {
        poems.push(linesHtml(body, table, tex, leaks));
        return "\u0005" + (poems.length - 1) + "\u0005";
      });
    }
    // tables, lifted out whole: a block of their own
    const tables = [];
    t = t.replace(/(^|\n)\{\|([^\n]*)\n([\s\S]*?)\n\|\}(?=\n|$)/g, (m, lead, attrs, body) => {
      tables.push(tableHtml(attrs, body, table, tex, leaks));
      return lead + "\n\u0004" + (tables.length - 1) + "\u0004\n\n";
    });
    t = afterBlocks(templates(t, table), table);
    // paragraphs: a blank line; any other newline is a space
    const paras = t.split(/\n[ \t]*\n\s*/).map(p => p.replace(/\s*\n\s*/g, " "));
    const out = paras.map(p => {
      const lone = p.trim().match(/^\u0004(\d+)\u0004$/);
      return lone ? tables[+lone[1]] : inline(p, table, tex, leaks);
    });
    const html = out.filter(p => p !== "").join(table.paragraph)
      .replace(/\u0005(\d+)\u0005/g, (m, i) => poems[+i])
      .replace(/\u0006(\d+)\u0006/g, (m, i) => images[+i])
      .split(ELEMENT).join("");
    return { html: html, leaks: leaks };
  }

  const api = { render: render, curlyQuotes: curlyQuotes };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.DeskRender = api;
})(typeof window !== "undefined" ? window : this);
