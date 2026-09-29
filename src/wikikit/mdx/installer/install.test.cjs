const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'), path=require('node:path'), os=require('node:os'), crypto=require('node:crypto');
const {install,configure,profileCandidates,dictionaryId,within}=require('./install.cjs');
const {DOMParser}=require('@xmldom/xmldom');
// What the build writes into manifest.book for EB1911 — the installer's only
// source of the book's names.
const BOOK={short_name:'Britannica 11',search_name:'Britannica title search',help_word:'Britannica 11',
  file_stem:'Britannica11',basename:'Britannica11',key_prefix:'EB1911:',slug:'eb1911'};
const SAMPLE_BOOK={...BOOK,help_word:'Britannica 11 sample',basename:'Britannica11-sample'};

test('shared configuration preserves settings and deduplicates program sources',()=>{
  const content=path.resolve('reader & names/content'), node=path.join(content,'search/runtime/node.exe');
  const xml='<config><preferences><zoomFactor>1.5</zoomFactor></preferences><programs><program id="other" enabled="0"/></programs><paths><path recursive="1">old</path></paths></config>';
  const result=configure(configure(xml,content,node,false,BOOK),content,node,false,BOOK);
  const doc=new DOMParser().parseFromString(result,'text/xml');
  assert.equal(doc.getElementsByTagName('program').length,3);
  assert.equal(doc.getElementsByTagName('zoomFactor')[0].textContent,'1.5');
  assert.equal(doc.getElementsByTagName('path').length,2);
  assert.equal(doc.getElementsByTagName('ignoreDiacritics')[0].textContent,'1');
  assert.equal(doc.getElementsByTagName('program')[1].getAttribute('commandLine').includes(node),true);
  assert.equal(result.includes('%GDWORD%'),false);
  assert.throws(()=>configure('<broken>',content,node,true,BOOK));
  // The names in GoldenDict are the book's, as before this came from the manifest.
  const programs=[...doc.getElementsByTagName('program')].filter(p=>p.getAttribute('id')!=='other');
  assert.deepEqual(programs.map(p=>[p.getAttribute('id'),p.getAttribute('name')]),
    [['eb1911-title-search','Britannica title search'],['eb1911-name-lookup','Britannica 11']]);
});
test('another book gets its own program ids and names, beside the first',()=>{
  const content=path.resolve('reader/content'), node=path.join(content,'search/runtime/node.exe');
  const dnb={...BOOK,short_name:'DNB',search_name:'DNB title search',file_stem:'DNB',slug:'dnb'};
  const both=configure(configure('<config/>',content,node,true,BOOK),content,node,true,dnb);
  const ids=[...new DOMParser().parseFromString(both,'text/xml').getElementsByTagName('program')].map(p=>p.getAttribute('id'));
  assert.deepEqual(ids.sort(),['dnb-name-lookup','dnb-title-search','eb1911-name-lookup','eb1911-title-search']);
  assert.notEqual(dictionaryId('/x',true,BOOK),dictionaryId('/x',true,dnb));
});
test('portable resource IDs match GoldenDict; ordinary profiles use absolute paths',()=>{
  // Unchanged by the move to manifest names: GoldenDict keys settings on this id.
  assert.equal(dictionaryId('/anywhere',true,BOOK),'149fc35a1954586eac330c2aa7075e74');
  assert.notEqual(dictionaryId(path.resolve('one'),false,BOOK),dictionaryId(path.resolve('two'),false,BOOK));
  assert.equal(profileCandidates('darwin','home',{}).length,1);
  assert.equal(profileCandidates('linux','home',{XDG_CONFIG_HOME:'custom'})[1],path.join('custom','goldendict','config'));
  assert.equal(profileCandidates('win32','home',{APPDATA:'appdata'})[0],path.join('appdata','GoldenDict','config'));
  assert.throws(()=>within(path.resolve('package'),'../escape'));
});
test('installation, update, damaged package rejection, and rollback',async()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'britannica-installer-test-'));
  const packageRoot=path.join(root,'package'), reader=path.join(root,'Reader & Unicode é');
  fs.mkdirSync(reader,{recursive:true}); fs.writeFileSync(path.join(reader,'goldendict.exe'),'fixture');
  fs.mkdirSync(path.join(reader,'portable')); fs.writeFileSync(path.join(reader,'portable/config'),'<config><preferences><zoomFactor>1.75</zoomFactor></preferences></config>');
  const runtime=process.platform==='win32'?'node.exe':'node';
  const names=['manifest.json','Britannica11.mdx','Britannica11.mdd','search/lookup.cjs','search/search-api.js','search/book.json','search/titles.json','search/articles.sqlite','search/runtime/'+runtime,'search/runtime/LICENSE'];
  for(const name of names){const file=within(packageRoot,name);fs.mkdirSync(path.dirname(file),{recursive:true});if(name==='search/runtime/'+runtime)fs.copyFileSync(process.execPath,file);else fs.writeFileSync(file,'fixture');}
  fs.writeFileSync(path.join(packageRoot,'manifest.json'),JSON.stringify({edition:'complete',book:BOOK}));
  function checksums(){fs.writeFileSync(path.join(packageRoot,'SHA256SUMS'),names.map(name=>crypto.createHash('sha256').update(fs.readFileSync(within(packageRoot,name))).digest('hex')+'  '+name).join('\n'));}
  checksums();const options={package:packageRoot,reader};
  const quiet=()=>{};
  await assert.rejects(install(options,quiet,()=>true),/still running/);
  await install(options,quiet,()=>false);
  await install(options,quiet,()=>false);
  const config=fs.readFileSync(path.join(reader,'portable/config'),'utf8');
  assert(config.includes('<zoomFactor>1.75</zoomFactor>'));
  assert.equal((config.match(/id="eb1911-title-search"/g)||[]).length,1);
  fs.writeFileSync(path.join(packageRoot,'Britannica11.mdx'),'changed');
  await assert.rejects(install(options,quiet,()=>false),/Package check failed/);
  assert.equal(fs.readFileSync(path.join(reader,'content/Britannica11.mdx'),'utf8'),'fixture');
  checksums();
  const rename=fs.renameSync;let failed=false;
  fs.renameSync=(source,dest)=>{if(!failed && source.includes(path.join('new','search','lookup.cjs'))){failed=true;throw Error('simulated commit failure');}return rename(source,dest);};
  try { await assert.rejects(install(options,quiet,()=>false),/simulated commit failure/); } finally {fs.renameSync=rename;}
  assert(failed);
  assert.equal(fs.readFileSync(path.join(reader,'content/Britannica11.mdx'),'utf8'),'fixture');
  assert.equal(fs.readFileSync(path.join(reader,'portable/config'),'utf8'),config);
  fs.writeFileSync(path.join(packageRoot,'manifest.json'),JSON.stringify({edition:'sample',book:SAMPLE_BOOK})); checksums();
  await assert.rejects(install(options,quiet,()=>false),/complete dictionary is already installed/);
  const trial=path.join(root,'trial'); fs.mkdirSync(trial); fs.writeFileSync(path.join(trial,'goldendict.exe'),'fixture');
  await install({package:packageRoot,reader:trial},quiet,()=>false);
  assert.equal(JSON.parse(fs.readFileSync(path.join(trial,'content/manifest.json'))).edition,'sample');
  fs.writeFileSync(path.join(packageRoot,'manifest.json'),JSON.stringify({edition:'complete',book:BOOK})); checksums();
  // A package that does not say which book it holds is refused, not guessed at.
  fs.writeFileSync(path.join(packageRoot,'manifest.json'),JSON.stringify({edition:'complete'})); checksums();
  await assert.rejects(install({package:packageRoot,reader:trial},quiet,()=>false),/which book it holds/);
  fs.writeFileSync(path.join(packageRoot,'manifest.json'),JSON.stringify({edition:'complete',book:BOOK})); checksums();
  await install({package:packageRoot,reader:trial},quiet,()=>false);
  assert.equal(JSON.parse(fs.readFileSync(path.join(trial,'content/manifest.json'))).edition,'complete');
  assert.equal((fs.readFileSync(path.join(trial,'portable/config'),'utf8').match(/id="eb1911-title-search"/g)||[]).length,1);
  // Only remove the temporary tree allocated by this test.
  assert.equal(path.dirname(root),fs.realpathSync(os.tmpdir()));
  assert(path.basename(root).startsWith('britannica-installer-test-'));
  fs.rmSync(root,{recursive:true});
});
