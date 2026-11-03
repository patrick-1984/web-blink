#!/usr/bin/env node
/** Explicit, one-time runtime preparation. Never called by session hooks or ordinary MCP startup. */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {privateDirectory} from './private-dir.mjs';
import {bootstrapUV} from './bootstrap-uv.mjs';

const root=path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const home=path.resolve(process.env.BLINK_HOME||path.join(os.homedir(),'.web-blink'));
const directory=path.join(home,'runtime');
const cache=path.join(directory,'uv-cache');
const python=path.join(directory,'python');
const marker=path.join(directory,'ready.json');
const machine=process.argv.includes('--json');
const say=text=>{if(!machine)process.stdout.write(text+'\n');};
const result=(status,summary,data={})=>{
 if(machine)process.stdout.write(JSON.stringify({status,summary,data,warnings:[]})+'\n');
 else process.stdout.write(summary+'\n');
 process.exitCode=status==='ok'?0:1;
};
say('Preparing Blink on this computer. This may download Python and the required libraries once.');
say('Files will be stored in: '+directory);
say('This step does not open a browser or read browser logins.');
try{
 const available=process.env.BLINK_TEST_BOOTSTRAP_UV==='1'?{status:1}:spawnSync('uv',['--version'],{stdio:'ignore',windowsHide:true,timeout:3000});
 const uv=available.error||available.status!==0?await bootstrapUV(directory):'uv';
 {
  privateDirectory(directory);
  const prepared=spawnSync(uv,['--cache-dir',cache,'run','--no-project','--with-requirements',path.join(root,'requirements.lock'),
    'python','-c','import sys,jsonschema; assert sys.version_info >= (3,11)'],
   {cwd:root,env:{...process.env,UV_PYTHON_INSTALL_DIR:python,PYTHONUTF8:'1',PYTHONIOENCODING:'utf-8'},
    stdio:'ignore',windowsHide:true,timeout:240000});
  if(prepared.error||prepared.status!==0){
   result('env_unavailable','Blink setup could not finish. If Codex or Windows blocked access, approve this one setup action and retry; otherwise check network access.',
    {directory,reason:prepared.error?.code==='ETIMEDOUT'?'timeout':'setup_failed'});
  }else{
   if(fs.existsSync(marker)&&fs.lstatSync(marker).isSymbolicLink())throw new Error('unsafe-path');
   const temporary=path.join(directory,`.ready-${process.pid}-${Date.now()}.json`);
   fs.writeFileSync(temporary,JSON.stringify({schemaVersion:1,cache:'app-local'})+'\n',{mode:0o600,flag:'wx'});
   fs.renameSync(temporary,marker);
   result('ok','Blink is ready. Continue with your browser connection; ordinary startup stays offline.',{directory,cacheDir:cache});
  }
 }
}catch(error){
 result('env_unavailable',error.message==='unsafe-path'?'Choose a Blink data folder outside a Git repository without linked directories.'
  :'Blink could not create its private setup folder. If Codex asks for permission to write outside the project, allow this one setup action and retry.',{directory});
}
