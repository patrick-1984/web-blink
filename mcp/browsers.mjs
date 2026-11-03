/** Windows profile metadata discovery. No Python, daemon, browser launch or credential reads. */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
import {privateDirectory} from './private-dir.mjs';

const require=createRequire(import.meta.url);

const extensionId='mmlmfjhmonkocbjadbfplnigmagldckm';
const extensionUrl='https://chromewebstore.google.com/detail/playwright-extension/'+extensionId;
function read(file,limit=4194304){
 let fd;
 try{fd=fs.openSync(file,'r');const stat=fs.fstatSync(fd);if(!stat.isFile()||stat.size>limit)return '';
  const buffer=Buffer.alloc(limit+1),count=fs.readSync(fd,buffer,0,limit+1,0);return count<=limit?buffer.subarray(0,count).toString('utf8'):'';
 }catch{return '';}finally{if(fd!==undefined)fs.closeSync(fd);}
}
function json(file){try{if(fs.lstatSync(file).isSymbolicLink())return {};const value=JSON.parse(read(file));return value&&typeof value==='object'&&!Array.isArray(value)?value:{};}catch{return {};}}
function directory(file){try{const info=fs.lstatSync(file);return info.isDirectory()&&!info.isSymbolicLink();}catch{return false;}}
function children(file){try{return fs.readdirSync(file).slice(0,1000);}catch{return [];}}
function label(value,fallback){return typeof value==='string'?value.replace(/[\x00-\x1f\x7f]/g,'').slice(0,150)||fallback:fallback;}
export function normalizeProfileName(value){return String(value).normalize('NFKC').toLowerCase().replace(/[^\p{L}\p{N}]+/gu,' ').trim();}
function profileNames(info,prefs,directory){
 info=info&&typeof info==='object'?info:{};const profile=prefs.profile||{},sources=[];
 const add=(source,value)=>{value=typeof value==='string'?label(value.trim(),''):'';
  if(value&&!sources.some(x=>x.value===value))sources.push({source,value});return value;};
 let local=add('Local State.enterprise_label',info.enterprise_label);
 const cached=add('Local State.name',info.name),pref=add('Preferences.profile.name',profile.name);
 const shortcut=add('Local State.shortcut_name',info.shortcut_name)||add('Preferences.profile.shortcut_name',profile.shortcut_name);
 let gaia=add('Local State.gaia_given_name',info.gaia_given_name)||add('Local State.gaia_name',info.gaia_name);
 for(const section of ['account_info','edge_account_info','edge_account','edge_profile']){
  let entries=prefs[section];entries=Array.isArray(entries)?entries:[entries];
  for(const entry of entries.slice(0,10))if(entry&&typeof entry==='object')
   for(const key of ['profile_name','display_name','full_name','given_name','name']){
    const value=add(`Preferences.${section}.${key}`,entry[key]);
    if(!gaia&&['given_name','full_name','display_name'].includes(key))gaia=value;
   }
 }
 const generic=x=>!x||/^(?:default|profile|person|profil|osoba)(?:\s*\d+)?$/i.test(x);
 local=local||[pref,cached].find(x=>!generic(x))||cached||pref||directory;
 const aliases=sources.map(x=>x.value);
 const combined=gaia&&normalizeProfileName(gaia)!==normalizeProfileName(local)&&!generic(local)&&!normalizeProfileName(local).startsWith(normalizeProfileName(gaia)+' ');
 if(combined)
  aliases.push(`${gaia} (${local})`,`${gaia} - ${local}`,`${gaia} – ${local}`);
 const name=shortcut&&!generic(shortcut)?shortcut:combined
  ?`${gaia} (${local})`:gaia&&(generic(local)||info.is_using_default_name===true)?gaia:local;
 return {name,displayName:name,aliases:[...new Set([name,...aliases,directory])],nameSources:sources,directoryName:directory};
}
function stamps(paths){return [...new Set(paths)].sort().map(file=>{try{const s=fs.statSync(file,{bigint:true});
 return [file,s.mtimeNs.toString(),Number(s.size)];}catch{return [file,null,null];}});}
function indexStore(home,env,refresh){
 const directory=path.join(env.BLINK_HOME||path.join(home,'.web-blink'),'cache'),file=path.join(directory,'browser-profiles.json');
 let document=json(file);if(document.schemaVersion!==2||document.platform!=='win32'||!document.entries)document={schemaVersion:2,platform:'win32',entries:{}};
 if(!document.numbers||typeof document.numbers!=='object'||Object.values(document.numbers).some(n=>!Number.isSafeInteger(n)||n<1))document.numbers={};
 document.nextNumber=Math.max(0,...Object.values(document.numbers))+1;
 let changed=false;const updated=new Set();
 const number=browser=>{for(const profile of browser.profiles||[]){
  if(!document.numbers[profile.id]){document.numbers[profile.id]=document.nextNumber++;changed=true;}
  profile.number=document.numbers[profile.id];profile.reference='#'+profile.number;
 }return browser;};
 return {path:file,get(kind){try{const entry=document.entries[kind];if(refresh||!entry||typeof entry.scannedAt!=='number'||Date.now()/1000-entry.scannedAt>300||!Array.isArray(entry.fingerprint))return null;
  return JSON.stringify(stamps(entry.fingerprint.map(x=>x[0])))===JSON.stringify(entry.fingerprint)?number(entry.browser):null;}catch{return null;}},
 put(kind,browser,paths){document.entries[kind]={scannedAt:Date.now()/1000,fingerprint:stamps(paths),browser:number(browser)};changed=true;updated.add(kind);},
 save(){if(!changed)return 'cached';let temporary,locked=false;const lock=path.join(directory,'.profile-index.lock');
  try{privateDirectory(directory);if(fs.existsSync(file)&&fs.lstatSync(file).isSymbolicLink())throw Error('unsafe-path');
   const deadline=Date.now()+2000;while(Date.now()<deadline){try{fs.mkdirSync(lock,{mode:0o700});locked=true;break;}catch(error){
    if(error.code!=='EEXIST')throw error;try{if(Date.now()-fs.statSync(lock).mtimeMs>300000)fs.rmdirSync(lock);}catch{}
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0,10);
   }}if(!locked)return 'memory-only';
   const latest=json(file);if(latest.schemaVersion===2&&latest.platform==='win32'){
    if(latest.numbers&&typeof latest.numbers==='object'&&Object.values(latest.numbers).every(n=>Number.isSafeInteger(n)&&n>0)){
     document.numbers=latest.numbers;document.nextNumber=Math.max(0,...Object.values(document.numbers))+1;
    }
    const merged=latest.entries||{};for(const [kind,entry] of Object.entries(document.entries)){
     if(updated.has(kind)||!merged[kind])merged[kind]=entry;if(entry.browser)number(entry.browser);
    }document.entries=merged;for(const entry of Object.values(merged))if(entry.browser)number(entry.browser);
   }
   temporary=path.join(directory,`.profiles-${process.pid}-${Date.now()}.json`);fs.writeFileSync(temporary,JSON.stringify(document)+'\n',{mode:0o600,flag:'wx'});
   fs.renameSync(temporary,file);return 'refreshed';
  }catch{return 'memory-only';}finally{try{if(temporary)fs.unlinkSync(temporary);}catch{}try{if(locked)fs.rmdirSync(lock);}catch{}}}};
}

export function discoverWindows(browser,{home=os.homedir(),env=process.env,refresh=false}={}){
 const kinds=['chrome','edge','brave','firefox'];
 if(browser!==undefined&&!kinds.includes(browser))throw new Error('Unknown browser');
 const local=env.LOCALAPPDATA||path.join(home,'AppData','Local');
 const roaming=env.APPDATA||path.join(home,'AppData','Roaming');
 const roots={chrome:path.join(local,'Google','Chrome','User Data'),edge:path.join(local,'Microsoft','Edge','User Data'),
  brave:path.join(local,'BraveSoftware','Brave-Browser','User Data'),firefox:path.join(roaming,'Mozilla','Firefox')};
 const bins={chrome:['Google','Chrome','Application','chrome.exe'],edge:['Microsoft','Edge','Application','msedge.exe'],
  brave:['BraveSoftware','Brave-Browser','Application','brave.exe'],firefox:['Mozilla Firefox','firefox.exe']};
 const index=indexStore(home,env,refresh);
 const listings=(browser?[browser]:kinds).map(kind=>{
  const cached=index.get(kind);if(cached)return cached;
  const root=roots[kind],profiles=new Map(),dependencies=[root,path.join(root,'Local State'),path.join(root,'profiles.ini'),path.join(root,'Profile Groups')];
  if(kind==='firefox'){
   let entry;
   for(const line of read(path.join(root,'profiles.ini'),1048576).split(/\r?\n/)){
    const section=line.trim().match(/^\[([^\]]+)\]$/);
    if(section){entry=section[1].startsWith('Profile')?{}:null;if(entry)profiles.set(Symbol(),entry);continue;}
    const pair=line.match(/^\s*([^=;#]+?)\s*=\s*(.*?)\s*$/);
    if(entry&&pair)entry[pair[1]]=pair[2];
   }
   const configured=[...profiles.values()];profiles.clear();
   for(const entry of configured){if(!entry.Path)continue;const full=path.resolve(entry.IsRelative==='0'?entry.Path:path.join(root,entry.Path));
    if(directory(full))profiles.set(full,{name:label(entry.Name,path.basename(full)),directory:path.basename(full)});}
   for(const base of [root,path.join(root,'Profiles')])for(const name of children(base)){
    const full=path.join(base,name);if(directory(full)&&fs.existsSync(path.join(full,'compatibility.ini'))&&!profiles.has(full))profiles.set(full,{name,directory:name});}
   for(const name of children(path.join(root,'Profile Groups')).filter(x=>x.endsWith('.sqlite'))){
    const file=path.join(root,'Profile Groups',name);dependencies.push(file,file+'-wal');let db;
    try{if(fs.lstatSync(file).isSymbolicLink())continue;
     const sqlite=require('node:sqlite'),Database=sqlite.DatabaseSync||sqlite.Database;
     db=new Database(file,{readOnly:true,allowExtension:false});db.exec('PRAGMA query_only=ON');
     for(const row of db.prepare('SELECT path,name FROM Profiles LIMIT 1000').all()){
      if(typeof row.path!=='string'||typeof row.name!=='string')continue;
      const full=path.resolve(path.isAbsolute(row.path)?row.path:path.join(root,row.path));
      if(directory(full))profiles.set(full,{name:label(row.name,path.basename(full)),directory:path.basename(full),groupSource:file});
     }
    }catch{}finally{try{db?.close();}catch{}}
   }
  }else{
   const cache=json(path.join(root,'Local State')).profile?.info_cache||{};
   const names=new Set([...Object.keys(cache),...children(root).filter(name=>fs.existsSync(path.join(root,name,'Preferences')))]);
   for(const name of names){if(!name||name==='.'||name==='..'||/[\\/]/.test(name))continue;
    const full=path.join(root,name);if(directory(full))profiles.set(full,{...profileNames(cache[name],{...json(path.join(full,'Secure Preferences')),...json(path.join(full,'Preferences'))},name),directory:name});}
  }
  const binary=[env.PROGRAMFILES,env['PROGRAMFILES(X86)'],local].filter(Boolean).map(base=>path.join(base,...bins[kind])).find(file=>{
   try{return fs.statSync(file).isFile();}catch{return false;}})||null;
  const found=[...profiles].map(([full,meta])=>{
   dependencies.push(path.join(full,'Preferences'),path.join(full,'Secure Preferences'),path.join(full,'Extensions'),path.join(full,'compatibility.ini'));
   const record=json(path.join(full,'Preferences')).extensions?.settings?.[extensionId]||json(path.join(full,'Secure Preferences')).extensions?.settings?.[extensionId];
   const installed=kind!=='firefox'&&(directory(path.join(full,'Extensions',extensionId))||!!record);
   const names=kind==='firefox'?{...meta,displayName:meta.name,aliases:[...new Set([meta.name,meta.directory])],directoryName:meta.directory,
    nameSources:[{source:meta.groupSource||'profiles.ini',value:meta.name}]}:meta;
   return {id:kind+'-'+createHash('sha256').update(full).digest('hex').slice(0,20),...names,path:full,userDataDir:root,
    running:'unknown',extensionInstalled:installed,attachSupported:kind!=='firefox',
    extensionEnabled:record?.state===1?true:record?.state===0?false:null,
    connection:kind==='firefox'?'har-import':installed?'extension-detected':'setup-required'};
  }).sort((a,b)=>a.directory.localeCompare(b.directory));
  const record={browser:kind,name:{chrome:'Chrome',edge:'Edge',brave:'Brave',firefox:'Firefox'}[kind],executable:binary,
   detected:!!binary||found.length>0,profiles:found,recording:kind==='firefox'?'har-import':'extension-or-cdp',
   note:kind==='firefox'?'Stock Firefox needs its own adapter; a separate PW browser is also available.':'Choose this profile by its display name; Blink prepares the selected connection.'};
  index.put(kind,record,dependencies);return record;
 });
 const blinkHome=env.BLINK_HOME||path.join(home,'.web-blink');
 const config=env.BLINK_CONFIG_DIR||path.join(blinkHome,'config');
 const document=json(path.join(config,'settings.json'));
 // Preferences are metadata, not arbitrary caller-provided commands or credentials.
 let selected={mode:'shared'};
 if(document.browser&&typeof document.browser==='object'){
  const allowed={shared:[],native:['browser','profile'],profile:['browser','profile'],playwright:['browser','name'],extension:['browser','profile'],cdp:['endpoint'],managed:['browser','name'],credentials:[]};
  const fields=allowed[document.browser.mode];
  if(fields&&fields.every(key=>typeof document.browser[key]==='string'&&document.browser[key].length<=500)){
   selected={mode:document.browser.mode};for(const key of fields)selected[key]=document.browser[key];
  }
 }
 const cacheStatus=index.save();
 return {browsers:listings,host:os.hostname(),platform:'win32',runtimeSupported:true,extensionUrl,
  profileRows:listings.flatMap(b=>b.profiles.map(p=>({id:p.reference,browser:b.browser,profile:p.displayName||p.name,profileId:p.id,directory:p.directory}))).sort((a,b)=>Number(a.id.slice(1))-Number(b.id.slice(1))),
  profileTable:'| ID | Browser | Profile |\n|---|---|---|\n'+listings.flatMap(b=>b.profiles.map(p=>({n:p.number,line:'| '+[p.reference,b.browser,p.displayName||p.name].map(x=>String(x).replaceAll('|','\\|').replace(/[\r\n]/g,' ')).join(' | ')+' |'}))).sort((a,b)=>a.n-b.n).map(x=>x.line).join('\n'),
  index:{path:index.path,status:cacheStatus,maxAgeSeconds:300,contents:'Profile names, aliases, paths and capabilities only; no cookies or credentials.'},
  selected,selectionSaved:!!document.browser,
  connection:{selection:selected,available:null,reason:'runtime-check-on-use',instructions:'Discovery needs Node only. Run blink_doctor to check the native Windows runtime before connecting.'},
  discovery:'Local profile metadata only. A discovered profile is not proof of a running browser or verified account.'};
}
