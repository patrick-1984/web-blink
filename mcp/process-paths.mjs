/** Code and custom interpreters must survive replacement of the host's plugin directory. */
import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {privateDirectory} from './private-dir.mjs';

export const inside=(file,root)=>{const relative=path.relative(path.resolve(root),path.resolve(file));return relative===''||(!relative.startsWith('..'+path.sep)&&relative!=='..'&&!path.isAbsolute(relative));};

export function stagePackage(root,home){
 const source=path.join(root,'src','blink');
 const files=[];
 const walk=(folder,prefix='')=>{for(const item of fs.readdirSync(folder,{withFileTypes:true}).sort((a,b)=>a.name.localeCompare(b.name))){
  if(item.name==='__pycache__'||item.name.endsWith('.pyc'))continue;
  const relative=path.join(prefix,item.name),file=path.join(folder,item.name);
  if(item.isSymbolicLink())throw Error('unsafe-path');
  if(item.isDirectory())walk(file,relative);else if(item.isFile())files.push([relative,fs.readFileSync(file)]);
 }};
 walk(source);
 const hash=createHash('sha256');for(const [name,data] of files)hash.update(name).update('\0').update(data);
 const base=path.join(home,'runtime','components');privateDirectory(base);
 const target=path.join(base,'node-'+hash.digest('hex').slice(0,24));
 if(!fs.existsSync(target)){
  const temporary=fs.mkdtempSync(path.join(base,'.stage-'));
  try{
   for(const [name,data] of files){const file=path.join(temporary,'blink',name);fs.mkdirSync(path.dirname(file),{recursive:true});fs.writeFileSync(file,data);}
   try{fs.renameSync(temporary,target);}catch(error){if(!fs.existsSync(target))throw error;}
  }finally{fs.rmSync(temporary,{recursive:true,force:true});}
 }
 return target;
}

export function externalPython(command,root,home){
 // uv's managed interpreter is already external. A configured plugin-local venv needs relocating.
 let binary;
 if(!path.isAbsolute(command)&&!command.includes(path.sep)){
  const suffixes=process.platform==='win32'?['',...(process.env.PATHEXT||'.EXE;.CMD;.BAT').split(';')]:[''];
  const found=(process.env.PATH||'').split(path.delimiter).flatMap(folder=>suffixes.map(suffix=>path.join(folder,command+suffix))).find(file=>fs.existsSync(file));
  if(found)command=found;
 }
 try{binary=fs.realpathSync(command);}catch{return command;}
 const candidate=path.resolve(command);
 const prefix=['bin','Scripts'].includes(path.basename(path.dirname(candidate)))?path.dirname(path.dirname(candidate)):path.dirname(candidate);
 let runtimeRoot=root;
 for(let parent=prefix;;parent=path.dirname(parent)){
  if(fs.existsSync(path.join(parent,'.claude-plugin/plugin.json'))){runtimeRoot=parent;break;}
  if(path.dirname(parent)===parent)break;
 }
 if(!inside(binary,runtimeRoot)&&!inside(prefix,runtimeRoot))return command;
 const base=path.join(home,'runtime','interpreters');privateDirectory(base);
 const stat=fs.statSync(candidate);
 let revision='';try{revision=fs.readFileSync(path.join(root,'.claude-plugin/plugin.json'),'utf8');}catch{}
 const key=createHash('sha256').update(prefix+'\0'+stat.size+'\0'+stat.mtimeMs+'\0'+revision).digest('hex').slice(0,24);
 const target=path.join(base,'node-'+key);
 if(!fs.existsSync(target)){
  const temporary=fs.mkdtempSync(path.join(base,'.python-'));
  try{
   fs.cpSync(prefix,path.join(temporary,'venv'),{recursive:true,dereference:true});
   const config=path.join(temporary,'venv','pyvenv.cfg');
   if(fs.existsSync(config)){
    let text=fs.readFileSync(config,'utf8');const pythonHome=text.match(/^home\s*=\s*(.+)$/m)?.[1]?.trim();
    if(pythonHome&&inside(pythonHome,runtimeRoot)){
     fs.cpSync(pythonHome,path.join(temporary,'base'),{recursive:true,dereference:true});
     text=text.split(pythonHome).join(path.join(target,'base'));fs.writeFileSync(config,text);
    }
   }
   try{fs.renameSync(temporary,target);}catch(error){if(!fs.existsSync(target))throw error;}
  }finally{fs.rmSync(temporary,{recursive:true,force:true});}
 }
 return path.join(target,'venv',path.relative(prefix,candidate));
}
