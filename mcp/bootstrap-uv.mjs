/** Fetch a pinned, checksum-verified uv only when explicit Blink preparation needs it. */
import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {privateDirectory} from './private-dir.mjs';

export async function bootstrapUV(directory){
 const binary=path.join(directory,'bin',process.platform==='win32'?'uv.exe':'uv');
 if(fs.existsSync(binary)&&fs.lstatSync(binary).isFile()&&!fs.lstatSync(binary).isSymbolicLink()){
  const check=spawnSync(binary,['--version'],{cwd:directory,stdio:'ignore',timeout:3000,windowsHide:true});
  if(!check.error&&check.status===0)return binary;
 }
 const pinned=JSON.parse(fs.readFileSync(fileURLToPath(new URL('./uv-assets.json',import.meta.url)),'utf8'));
 const cpu={x64:'x86_64',arm64:'aarch64'}[process.arch];
 const libc=process.platform==='linux'&&!process.report?.getReport().header.glibcVersionRuntime?'musl':'gnu';
 const suffix={win32:'pc-windows-msvc.zip',darwin:'apple-darwin.tar.gz',linux:'unknown-linux-'+libc+'.tar.gz'}[process.platform];
 const asset=`uv-${cpu}-${suffix}`,expected=pinned.assets[asset]?.sha256;
 if(!cpu||!suffix||!expected)throw Error('unsupported-platform');
 privateDirectory(directory);
 const temporary=fs.mkdtempSync(path.join(directory,'.uv-download-'));
 try{
  const response=await fetch(`https://github.com/astral-sh/uv/releases/download/${pinned.version}/${asset}`,{signal:AbortSignal.timeout(120000)});
  if(!response.ok||!response.body)throw Error('download-failed');
  const chunks=[];let size=0;const reader=response.body.getReader();
  while(true){const {done,value}=await reader.read();if(done)break;size+=value.length;if(size>67108864){await reader.cancel();throw Error('download-too-large');}chunks.push(Buffer.from(value));}
  const bytes=Buffer.concat(chunks);
  if(createHash('sha256').update(bytes).digest('hex')!==expected)throw Error('checksum-mismatch');
  const archive=path.join(temporary,asset),extract=path.join(temporary,'unpacked');
  fs.writeFileSync(archive,bytes,{mode:0o600});fs.mkdirSync(extract,{mode:0o700});
  const result=process.platform==='win32'
   ?spawnSync('powershell.exe',['-NoProfile','-NonInteractive','-Command','Expand-Archive -LiteralPath $env:BLINK_BOOTSTRAP_ARCHIVE -DestinationPath $env:BLINK_BOOTSTRAP_EXTRACT -Force'],
      {cwd:directory,env:{...process.env,BLINK_BOOTSTRAP_ARCHIVE:archive,BLINK_BOOTSTRAP_EXTRACT:extract},stdio:'ignore',windowsHide:true,timeout:30000})
   :spawnSync('tar',['-xf',archive,'-C',extract],{cwd:directory,stdio:'ignore',timeout:30000});
  if(result.error||result.status!==0)throw Error('unpack-failed');
  const find=folder=>{for(const item of fs.readdirSync(folder,{withFileTypes:true})){
   const file=path.join(folder,item.name);if(item.isFile()&&item.name===path.basename(binary))return file;
   if(item.isDirectory()){const nested=find(file);if(nested)return nested;}
  }return null;};
  const installed=find(extract);if(!installed)throw Error('binary-missing');
  privateDirectory(path.dirname(binary));
  if(fs.existsSync(binary)&&fs.lstatSync(binary).isSymbolicLink())throw Error('unsafe-path');
  fs.copyFileSync(installed,binary);if(process.platform!=='win32')fs.chmodSync(binary,0o700);
  return binary;
 }finally{fs.rmSync(temporary,{recursive:true,force:true});}
}
