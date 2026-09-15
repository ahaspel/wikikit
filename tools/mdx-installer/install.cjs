// Shared installation engine for Windows, macOS and Linux. No shell query interpolation.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const {execFileSync} = require('node:child_process');
const {DOMParser, XMLSerializer} = require('@xmldom/xmldom');
const IDS = ['eb1911-title-search', 'eb1911-name-lookup'];

function within(root, name) {
  const result = path.resolve(root, name), relative = path.relative(path.resolve(root), result);
  if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) throw Error('Invalid package path: '+name);
  return result;
}
async function hash(file) {
  const sha = crypto.createHash('sha256');
  for await (const chunk of fs.createReadStream(file)) sha.update(chunk);
  return sha.digest('hex');
}
function child(doc, parent, name) {
  let item = Array.from(parent.childNodes).find(n => n.nodeType === 1 && n.nodeName === name);
  if (!item) parent.appendChild(item = doc.createElement(name));
  return item;
}
function profileCandidates(platform = process.platform, home = os.homedir(), env = process.env) {
  if (platform === 'win32') return [path.join(env.APPDATA || home, 'GoldenDict', 'config')];
  const old = path.join(home, '.goldendict', 'config');
  if (platform === 'darwin') return [old];
  return [old, path.join(env.XDG_CONFIG_HOME || path.join(home,'.config'), 'goldendict', 'config')];
}
function dictionaryId(content, portable) {
  const names = ['Britannica11.mdd','Britannica11.mdx'].map(name => portable ? name : path.join(content,name).replace(/\\/g,'/'));
  return crypto.createHash('md5').update(names.sort().map(name=>name+'\0').join('')).digest('hex');
}
function running() {
  if (process.platform === 'win32') {
    return /goldendict\.exe/i.test(execFileSync('tasklist', ['/FI','IMAGENAME eq goldendict.exe','/FO','CSV','/NH'], {encoding:'utf8',windowsHide:true}));
  }
  const commands = execFileSync('ps', ['-A','-o','comm='], {encoding:'utf8'});
  return commands.split('\n').some(command => /^goldendict(?:-ng)?$/i.test(path.basename(command.trim())));
}

function configure(xml, content, node, portable) {
  const doc = new DOMParser({onError: level => { if (level !== 'warning') throw Error('Invalid GoldenDict configuration XML'); }}).parseFromString(xml, 'text/xml');
  if (doc.documentElement.nodeName !== 'config') throw Error('Expected a GoldenDict config file');
  const root = doc.documentElement, programs = child(doc, root, 'programs');
  for (const p of Array.from(programs.childNodes))
    if (p.nodeType === 1 && IDS.includes(p.getAttribute('id'))) programs.removeChild(p);
  const search = path.join(content, 'search');
  const quote = s => { if (/["\r\n]/.test(s)) throw Error('Unsupported quote or newline in installation path'); return '"'+s+'"'; };
  const command = quote(node)+' --no-warnings '+quote(path.join(search,'lookup.cjs'))+' '+quote(path.join(search,'titles.json'));
  IDS.forEach((id, i) => {
    const p = doc.createElement('program');
    const values = {id, type:i ? '2':'3', enabled:'1', name:i ? 'Britannica 11':'Britannica title search', icon:'',
      commandLine:command+(i ? ' --article '+quote(path.join(search,'articles.sqlite'))+' '+quote(path.join(search,'binding.json')):'')};
    for (const [key,value] of Object.entries(values)) p.setAttribute(key,value);
    programs.appendChild(p);
  });
  const preferences = child(doc,root,'preferences');
  child(doc,preferences,'ignoreDiacritics').textContent = '1';
  child(doc,child(doc,preferences,'fullTextSearch'),'enabled').textContent = '1';
  if (!portable) {
    const paths = child(doc,root,'paths');
    const existing = Array.from(paths.childNodes).some(p=>p.nodeType===1 && path.resolve(p.textContent)===content);
    if (!existing) { const p=doc.createElement('path'); p.setAttribute('recursive','1'); p.textContent=content; paths.appendChild(p); }
  }
  return new XMLSerializer().serializeToString(doc);
}

async function install(options, progress = console.log, isRunning = running) {
  const packageRoot = path.resolve(options.package);
  const portable = !!options.reader;
  let config, content;
  if (portable) {
    const reader = path.resolve(options.reader);
    if (!['goldendict.exe','goldendict','goldendict-ng'].some(name=>fs.existsSync(path.join(reader,name))))
      throw Error('Choose the folder containing the GoldenDict executable.');
    config = path.join(reader,'portable','config'); content=path.join(reader,'content');
  } else {
    config = options.config ? path.resolve(options.config) : profileCandidates().find(p=>fs.existsSync(p));
    if (!config || !fs.existsSync(config)) throw Error('Open GoldenDict once, quit it, then run setup again. Or specify --config PATH.');
    content = options.destination ? path.resolve(options.destination) : path.join(path.dirname(config),'dictionaries','Britannica11');
  }
  if (isRunning()) throw Error('GoldenDict is still running. Choose File > Quit, then try again.');
  progress('Checking the package…');
  const files = new Map();
  for (const line of fs.readFileSync(path.join(packageRoot,'SHA256SUMS'),'utf8').trim().split(/\r?\n/)) {
    const match = /^([0-9a-f]{64})  (.+)$/.exec(line);
    if (!match) throw Error('Invalid package checksums');
    const [,expected,name] = match;
    if (files.has(name) || await hash(within(packageRoot,name)) !== expected) throw Error('Package check failed: '+name);
    files.set(name, expected);
  }
  const runtime = process.platform === 'win32' ? 'node.exe':'node';
  for (const name of ['Britannica11.mdx','Britannica11.mdd','search/lookup.cjs','search/search-api.js',
    'search/titles.json','search/articles.sqlite','search/runtime/'+runtime,'search/runtime/LICENSE','manifest.json'])
    if (!files.has(name)) throw Error('Missing package file: '+name);
  const incoming = JSON.parse(fs.readFileSync(path.join(packageRoot,'manifest.json'),'utf8'));
  const installedManifest = path.join(content,'manifest.json');
  if (incoming.edition==='sample' && fs.existsSync(path.join(content,'Britannica11.mdx')) &&
      (!fs.existsSync(installedManifest) || JSON.parse(fs.readFileSync(installedManifest,'utf8')).edition!=='sample'))
    throw Error('The complete dictionary is already installed here. Use a separate reader folder to try the sample.');
  const node = path.join(content,'search','runtime',runtime);
  const bundled = path.join(packageRoot,'search','runtime',runtime);
  execFileSync(bundled, ['--no-warnings','-e',"const {DatabaseSync}=require('node:sqlite');new DatabaseSync(':memory:').close()"], {windowsHide:true, timeout:15000});
  const previous = fs.existsSync(config) ? fs.readFileSync(config,'utf8') : '<config/>';
  const updated = configure(previous, content, node, portable);
  fs.mkdirSync(path.dirname(content), {recursive:true});
  const stage = fs.mkdtempSync(path.join(path.dirname(content),'.britannica-install-'));
  const changes = [], committed=[];
  let cleanup=false;
  try {
    progress('Copying the dictionary and search files…');
    for (const name of files.keys()) {
      // Install book data and runtime; retain setup tools in the extracted package.
      if (!(name.startsWith('search/') || ['Britannica11.mdx','Britannica11.mdd','manifest.json','LICENSE','source-link-issues.json','installation.json'].includes(name))) continue;
      const source = within(stage,'new/'+name), dest=within(content,name);
      fs.mkdirSync(path.dirname(source),{recursive:true}); fs.copyFileSync(within(packageRoot,name),source);
      if (process.platform !== 'win32' && name==='search/runtime/node') fs.chmodSync(source,0o755);
      changes.push({source,dest});
    }
    const binding = within(stage,'new/binding.json');
    fs.writeFileSync(binding,JSON.stringify({dictionaryId:dictionaryId(content,portable)}));
    changes.push({source:binding,dest:path.join(content,'search','binding.json')});
    const stagedConfig = within(stage,'new/config'); fs.writeFileSync(stagedConfig,updated);
    changes.push({source:stagedConfig,dest:config});
    if (isRunning()) throw Error('GoldenDict was opened during installation. Quit it and try again.');
    if (fs.existsSync(config) && fs.readFileSync(config,'utf8')!==previous) throw Error('GoldenDict settings changed during installation. Try again.');
    if (fs.existsSync(config)) fs.copyFileSync(config,config+'.before-britannica-'+Date.now());
    progress('Finishing installation…');
    for (const [i,change] of changes.entries()) {
      const backup=within(stage,'old/'+i); fs.mkdirSync(path.dirname(backup),{recursive:true});
      fs.mkdirSync(path.dirname(change.dest),{recursive:true});
      if (fs.existsSync(change.dest)) fs.renameSync(change.dest,backup);
      committed.push({...change,backup}); fs.renameSync(change.source,change.dest);
    }
    cleanup=true;
  } catch (error) {
    for (const change of committed.reverse()) {
      if (fs.existsSync(change.dest)) fs.unlinkSync(change.dest);
      if (fs.existsSync(change.backup)) fs.renameSync(change.backup,change.dest);
    }
    cleanup=true; throw error;
  } finally {
    if (cleanup && path.dirname(stage)===path.dirname(content) && path.basename(stage).startsWith('.britannica-install-')) fs.rmSync(stage,{recursive:true});
  }
  progress('Installed. Open GoldenDict and look up '+(incoming.edition==='sample'?'Britannica 11 sample':'Britannica 11')+'.');
  return {config,content};
}

module.exports={install,configure,profileCandidates,dictionaryId,within};
if (require.main===module) {
  const args=process.argv.slice(2), options={package:path.resolve(__dirname,'..')};
  for (let i=0;i<args.length;i+=2) {
    if (!['--package','--reader','--config','--destination'].includes(args[i]) || !args[i+1]) throw Error('Invalid installation argument');
    options[args[i].slice(2)]=args[i+1];
  }
  install(options).catch(error=>{ console.error(error.message);process.exitCode=1; });
}
