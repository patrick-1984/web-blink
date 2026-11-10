/** Keeps an explicitly launched personal profile alive independently of an MCP client's work tabs. */
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';

const [configFile,readyFile,library]=process.argv.slice(2);
let context;
const write=value=>{const temporary=readyFile+'.'+process.pid+'.tmp';fs.writeFileSync(temporary,JSON.stringify(value)+'\n',{mode:0o600,flag:'wx'});fs.renameSync(temporary,readyFile);};
try{
 const config=JSON.parse(fs.readFileSync(configFile,'utf8'));
 const playwright=createRequire(import.meta.url)(library);
 context=await playwright.chromium.launchPersistentContext(config.browser.userDataDir,config.browser.launchOptions);
 const browser=context.browser();
 const {endpoint}=await browser.bind('Web Blink existing profile',{workspaceDir:path.dirname(readyFile)});
 write({status:'ready',endpoint,pid:process.pid});
 browser.on('disconnected',()=>{
  try{if(JSON.parse(fs.readFileSync(readyFile,'utf8')).pid===process.pid)fs.unlinkSync(readyFile);}catch{}
  process.exit(0);
 });
}catch(error){
 const message=String(error?.message||'').toLowerCase();
 const code=/already in use|processsingleton|singletonlock|profile.*in use/.test(message)?'profile_in_use'
  :/non-default|default.*(?:profile|data directory)/.test(message)?'profile_protected':'profile_launch_failed';
 try{write({status:'error',code});}catch{}
 try{await context?.close();}catch{}
 process.exitCode=1;
}
