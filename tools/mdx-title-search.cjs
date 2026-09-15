// Native GoldenDict Prefix match helper. Query arrives on stdin, never a shell.
const fs = require('node:fs');
const path = require('node:path');
global.window = {};
require(fs.existsSync(path.join(__dirname, 'search-api.js')) ? './search-api.js' : './viewer/search-api.js');
const {fold, titleRank} = window.BritannicaSearch;
const indexPath = process.argv[2];
const query = fs.readFileSync(0, 'utf8').trim();
if (!query || query.startsWith('EB1911:')) process.exit(0);
const q = fold(query), terms = q.split(/[\s,.'’()\-]+/).filter(Boolean);
if (!terms.length) process.exit(0);
const records = JSON.parse(fs.readFileSync(indexPath, 'utf8'));
const matches = [];
for (const record of records) {
  let rank = Infinity;
  for (const name of record.names) {
    const folded = fold(name);
    const words = folded.split(/[\s,.'’()\-]+/).filter(Boolean);
    if (folded.includes(q) || terms.every(term => words.some(word => word.startsWith(term)))) {
      rank = Math.min(rank, titleRank(name, query));
    }
  }
  if (rank < Infinity) matches.push({...record, rank});
}
// One record per article identity; alternate spellings never become outputs.
matches.sort((a,b) => a.rank-b.rank || a.title.localeCompare(b.title));
if (process.argv[3] === '--article') {
  // Canonical queries are already served by the MDX; do not duplicate them.
  if (records.some(record => fold(record.title) === q)) process.exit(0);
  const exact = matches.filter(record => record.names.some(name => fold(name) === q));
  if (!matches.length) process.exit(0);
  const {DatabaseSync} = require('node:sqlite');
  const {inflateSync} = require('node:zlib');
  const db = new DatabaseSync(process.argv[4], {readOnly:true});
  const css = db.prepare('SELECT value FROM metadata WHERE key=?').get('css').value;
  const {dictionaryId} = JSON.parse(fs.readFileSync(process.argv[5], 'utf8'));
  const escape = s => s.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  if (exact.length !== 1) {
    const choices = exact.length ? exact : matches.slice(0,50);
    process.stdout.write('<style>'+css+'</style><div class="eb1911"><h1>Choose an article</h1><ul>'+choices.map(r =>
      `<li><a href="gdlookup://localhost/?word=${encodeURIComponent('EB1911:article:'+r.id)}&amp;group=4294967294">${escape(r.title)}</a></li>`).join('')+'</ul></div>');
  } else {
    const row = db.prepare('SELECT body FROM articles WHERE id=?').get(exact[0].id);
    if (!row) throw new Error('Missing article: '+exact[0].id);
    const body = inflateSync(row.body).toString('utf8');
    process.stdout.write('<style>' + css + '</style>' + body.replace(/<link\b[^>]*>/g, '').replace(/(src|href)="([^"]+)"/g, (match,attr,url) => {
      if (url.startsWith('entry://')) {
        const split = url.indexOf('#');
        const word = split < 0 ? url.slice(8) : url.slice(8,split);
        const anchor = split < 0 ? '' : '&amp;gdanchor='+url.slice(split+1);
        return `${attr}="gdlookup://localhost/?word=${word}&amp;group=4294967294${anchor}"`;
      }
      if (url.startsWith('#') || /^[a-z][a-z0-9+.-]*:/i.test(url)) return match;
      return `${attr}="bres://${dictionaryId}/${url}"`;
    }));
  }
  db.close();
  process.exit(0);
}
if (matches.length) process.stdout.write(matches.slice(0,50).map(record => record.title).join('\n') + '\n');
