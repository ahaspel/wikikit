"""Embedded full-text index for the EPUB's Full-Text Search page.

Article-granularity inverted index: folded term → sorted doc ids, delta+varint
encoded into JS string assets (``fts-data.js``).  AND-of-words queries decode and
intersect posting lists client-side — milliseconds against a ~30MB asset, vs the
reader's minutes-long linear crawl of ~500MB of XHTML.  Set-based (no positions):
no phrase queries, no snippets — the site's Meilisearch keeps those.

Terms present in more than DF_CAP articles are dropped from the index and listed
for the page to ignore in queries (ubiquitous words are unsearchable noise but a
double-digit share of the postings).

Encoding: doc-id deltas as little-endian 5-bit varint chunks; each char carries
5 payload bits + a continuation bit selected from a 64-char alphabet that needs
no escaping inside a CDATA JS string.  Posting lists are concatenated in term
order, "|"-separated, parallel to the space-separated sorted term string.
"""
import re
import unicodedata

DF_CAP_FRACTION = 0.27      # a term in >27% of docs is dropped (unsearchable noise;
                            # at full scale ≈ the measured 10k-doc knee)
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz+*"
_TOK_RE = re.compile(r"[a-z0-9]+")
_MARKER_RE = re.compile(r"«[^»]*»")


def fold(s):
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def tokens(body):
    """Distinct folded search tokens of a raw article body (markers stripped)."""
    return set(_TOK_RE.findall(fold(_MARKER_RE.sub(" ", body))))


def _varint(n, out):
    while True:
        chunk = n & 0x1F
        n >>= 5
        out.append(_ALPHABET[chunk | (0x20 if n else 0)])
        if not n:
            return


class Collector:
    """Feed one doc at a time (spine order); emit the JS asset at the end."""

    def __init__(self):
        self.docs = []            # [title, href] per doc id
        self.by_term = {}         # term -> [doc ids]

    def add(self, title, href, body):
        did = len(self.docs)
        self.docs.append([title, href])
        for t in tokens(body):
            self.by_term.setdefault(t, []).append(did)

    def asset_js(self):
        import json
        cap = max(200, int(len(self.docs) * DF_CAP_FRACTION))
        kept = sorted(t for t, ds in self.by_term.items() if len(ds) <= cap)
        dropped = sorted(t for t, ds in self.by_term.items() if len(ds) > cap)
        posts = []
        for t in kept:
            out = []
            prev = -1
            for did in self.by_term[t]:      # already ascending (spine feed order)
                _varint(did - prev - 1, out)
                prev = did
            posts.append("".join(out))
        docs_js = json.dumps(self.docs, ensure_ascii=False).replace("]]>", "]]\\u003e")
        return (f"var FTS_TERMS={json.dumps(' '.join(kept))};\n"
                f"var FTS_POSTS={json.dumps('|'.join(posts))};\n"
                f"var FTS_DROPPED={json.dumps(' '.join(dropped))};\n"
                f"var FTS_DOCS={docs_js};\n")


DECODER_JS = r"""
var FTS_MAP=null;
function ftsInit(){
 if(FTS_MAP)return;
 var terms=FTS_TERMS.split(" ");
 var posts=FTS_POSTS.split("|");
 FTS_MAP={};
 for(var i=0;i<terms.length;i++)FTS_MAP[terms[i]]=posts[i];
 var drop=FTS_DROPPED.split(" ");
 FTS_DROP={};
 for(var j=0;j<drop.length;j++)FTS_DROP[drop[j]]=1;
}
var FTS_DROP={};
var FTS_A="0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz+*";
var FTS_V={};for(var _i=0;_i<64;_i++)FTS_V[FTS_A.charAt(_i)]=_i;
var FTS_CACHE={};
function ftsDecode(term){
 if(FTS_CACHE[term])return FTS_CACHE[term];
 var s=FTS_MAP[term];
 if(s===undefined)return null;
 var ids=[],prev=-1,acc=0,shift=0;
 for(var i=0;i<s.length;i++){
  var v=FTS_V[s.charAt(i)];
  acc|=(v&31)<<shift;
  if(v&32){shift+=5;}
  else{prev=prev+acc+1;ids.push(prev);acc=0;shift=0;}
 }
 FTS_CACHE[term]=ids;
 return ids;
}
function ftsFold(s){return String(s||"").toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g,"");}
function ftsQuery(q){
 ftsInit();
 var toks=ftsFold(q).split(/[^a-z0-9]+/).filter(Boolean);
 var need=[],ignored=[];
 for(var i=0;i<toks.length;i++){
  if(FTS_DROP[toks[i]]){ignored.push(toks[i]);continue;}
  if(need.indexOf(toks[i])===-1)need.push(toks[i]);
 }
 if(!need.length)return {docs:[],ignored:ignored,empty:!toks.length};
 var lists=[];
 for(var k=0;k<need.length;k++){
  var l=ftsDecode(need[k]);
  if(!l)return {docs:[],ignored:ignored,miss:need[k]};
  lists.push(l);
 }
 lists.sort(function(a,b){return a.length-b.length;});
 var cur=lists[0];
 for(var m=1;m<lists.length;m++){
  var nx=lists[m],out=[],a=0,b=0;
  while(a<cur.length&&b<nx.length){
   if(cur[a]===nx[b]){out.push(cur[a]);a++;b++;}
   else if(cur[a]<nx[b])a++;else b++;
  }
  cur=out;
  if(!cur.length)break;
 }
 return {docs:cur,ignored:ignored,terms:need};
}
"""
