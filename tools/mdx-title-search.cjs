// Native GoldenDict Prefix match helper. Query arrives on stdin, never a shell.
const fs = require('node:fs');
global.window = {};
require('./viewer/search-api.js');
const {fold, titleRank} = window.BritannicaSearch;
const indexPath = process.argv[2];
const query = fs.readFileSync(0, 'utf8').trim();
if (!query) process.exit(0);
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
  // Prototype article resolution: canonical queries are served by the MDX.
  if (records.some(record => fold(record.title) === q)) process.exit(0);
  const bodies = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
  const dictionaryId = process.argv[5];
  const exact = matches.filter(record => record.names.some(name => fold(name) === q));
  for (const record of exact) {
    const body = bodies.entries[record.title];
    if (!body) continue;
    process.stdout.write('<style>' + bodies.css + '</style>' + body.replace(/<link\b[^>]*>/g, '').replace(/(src|href)="([^"#]+)"/g, (match,attr,url) => {
      if (url.startsWith('entry://')) return `${attr}="gdlookup://localhost/?word=${url.slice(8)}&amp;group=4294967294"`;
      if (/^[a-z][a-z0-9+.-]*:/i.test(url)) return match;
      return `${attr}="bres://${dictionaryId}/${url}"`;
    }));
  }
  process.exit(0);
}
if (matches.length) process.stdout.write(matches.slice(0,50).map(record => record.title).join('\n') + '\n');
