import fs from 'node:fs';
import path from 'node:path';
import {spawnSync} from 'node:child_process';

export function privateDirectory(target){
 for(let current=path.resolve(target);;current=path.dirname(current)){
  try{if(fs.lstatSync(current).isSymbolicLink())throw new Error('unsafe-path');}catch(error){if(error.code!=='ENOENT')throw error;}
  if(fs.existsSync(path.join(current,'.git')))throw new Error('unsafe-path');
  if(path.dirname(current)===current)break;
 }
 fs.mkdirSync(target,{recursive:true,mode:0o700});
 if(process.platform!=='win32'){fs.chmodSync(target,0o700);return;}
 const system=path.join(process.env.SystemRoot||'C:\\Windows','System32');
 const identity=spawnSync(path.join(system,'whoami.exe'),['/user','/fo','csv','/nh'],{cwd:target,encoding:'utf8',windowsHide:true,timeout:5000});
 const sid=identity.status===0?identity.stdout?.match(/S-1-\d+(?:-\d+)+/)?.[0]:null;
 if(!sid)throw new Error('private-directory');
 const result=spawnSync(path.join(system,'icacls.exe'),[target,'/inheritance:r','/grant:r',`*${sid}:(OI)(CI)F`,'*S-1-5-18:(OI)(CI)F'],
  {cwd:target,stdio:'ignore',windowsHide:true,timeout:5000});
 if(result.error||result.status!==0)throw new Error('private-directory');
}
