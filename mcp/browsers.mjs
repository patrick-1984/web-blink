/** Windows profile metadata discovery. No Python, daemon, browser launch or credential reads. */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';

const extensionId='mmlmfjhmonkocbjadbfplnigmagldckm';
const extensionUrl='https://chromewebstore.google.com/detail/playwright-extension/'+extensionId;
function read(file,limit=4194304){
 let fd;
 try{fd=fs.openSync(file,'r');const stat=fs.fstatSync(fd);if(!stat.isFile()||stat.size>limit)return '';
  const buffer=Buffer.alloc(limit+1),count=fs.readSync(fd,buffer,0,limit+1,0);return count<=limit?buffer.subarray(0,count).toString('utf8'):'';
 }catch{return '';}finally{if(fd!==undefined)fs.closeSync(fd);}
}
function json(file){try{const value=JSON.parse(read(file));return value&&typeof value==='object'&&!Array.isArray(value)?value:{};}catch{return {};}}
function directory(file){try{const info=fs.lstatSync(file);return info.isDirectory()&&!info.isSymbolicLink();}catch{return false;}}
function children(file){try{return fs.readdirSync(file).slice(0,1000);}catch{return [];}}
function label(value,fallback){return typeof value==='string'?value.replace(/[\x00-\x1f\x7f]/g,'').slice(0,150)||fallback:fallback;}

export function discoverWindows(browser,{home=os.homedir(),env=process.env}={}){
 const kinds=['chrome','edge','brave','firefox'];
 if(browser!==undefined&&!kinds.includes(browser))throw new Error('Unknown browser');
 const local=env.LOCALAPPDATA||path.join(home,'AppData','Local');
 const roaming=env.APPDATA||path.join(home,'AppData','Roaming');
 const roots={chrome:path.join(local,'Google','Chrome','User Data'),edge:path.join(local,'Microsoft','Edge','User Data'),
  brave:path.join(local,'BraveSoftware','Brave-Browser','User Data'),firefox:path.join(roaming,'Mozilla','Firefox')};
 const bins={chrome:['Google','Chrome','Application','chrome.exe'],edge:['Microsoft','Edge','Application','msedge.exe'],
  brave:['BraveSoftware','Brave-Browser','Application','brave.exe'],firefox:['Mozilla Firefox','firefox.exe']};
 const listings=(browser?[browser]:kinds).map(kind=>{
  const root=roots[kind],profiles=new Map();
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
  }else{
   const cache=json(path.join(root,'Local State')).profile?.info_cache||{};
   const names=new Set([...Object.keys(cache),...children(root).filter(name=>fs.existsSync(path.join(root,name,'Preferences')))]);
   for(const name of names){if(!name||name==='.'||name==='..'||/[\\/]/.test(name))continue;
    const full=path.join(root,name);if(directory(full))profiles.set(full,{name:label(cache[name]?.name,name),directory:name});}
  }
  const binary=[env.PROGRAMFILES,env['PROGRAMFILES(X86)'],local].filter(Boolean).map(base=>path.join(base,...bins[kind])).find(file=>{
   try{return fs.statSync(file).isFile();}catch{return false;}})||null;
  const found=[...profiles].map(([full,meta])=>{
   const installed=kind!=='firefox'&&directory(path.join(full,'Extensions',extensionId));
   return {id:kind+'-'+createHash('sha256').update(full).digest('hex').slice(0,20),...meta,path:full,userDataDir:root,
    running:'unknown',extensionInstalled:installed,attachSupported:kind!=='firefox',
    connection:kind==='firefox'?'har-import':installed?'extension-detected':'setup-required'};
  }).sort((a,b)=>a.directory.localeCompare(b.directory));
  return {browser:kind,name:{chrome:'Chrome',edge:'Edge',brave:'Brave',firefox:'Firefox'}[kind],executable:binary,
   detected:!!binary||found.length>0,profiles:found,recording:kind==='firefox'?'har-import':'extension-or-cdp',
   note:kind==='firefox'?'Export a HAR from Firefox DevTools; stock Firefox live attachment is unavailable.':'Install the Playwright extension in the chosen profile and approve its connection.'};
 });
 const blinkHome=env.BLINK_HOME||path.join(home,'.web-blink');
 const config=env.BLINK_CONFIG_DIR||path.join(blinkHome,'config');
 const document=json(path.join(config,'settings.json'));
 // Preferences are metadata, not arbitrary caller-provided commands or credentials.
 let selected={mode:'shared'};
 if(document.browser&&typeof document.browser==='object'){
  const allowed={shared:[],extension:['browser','profile'],cdp:['endpoint'],managed:['browser','name'],credentials:[]};
  const fields=allowed[document.browser.mode];
  if(fields&&fields.every(key=>typeof document.browser[key]==='string'&&document.browser[key].length<=500)){
   selected={mode:document.browser.mode};for(const key of fields)selected[key]=document.browser[key];
  }
 }
 return {browsers:listings,host:os.hostname(),platform:'win32',runtimeSupported:true,extensionUrl,
  selected,selectionSaved:!!document.browser,
  connection:{selection:selected,available:null,reason:'runtime-check-on-use',instructions:'Discovery needs Node only. Run blink_doctor to check the native Windows runtime before connecting.'},
  discovery:'Local profile metadata only. A discovered profile is not proof of a running browser or verified account.'};
}
