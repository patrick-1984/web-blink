#!/usr/bin/env node
/** Claude-compatible MCP stdio server. Diagnostics require only this Node file. */
import {spawn, spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import {StringDecoder} from 'node:string_decoder';

const root = process.env.BLINK_PLUGIN_ROOT || path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const LIMIT = 1048576;
const PROTOCOLS = ['2025-11-25', '2025-06-18', '2024-11-05'];
const textSchema = {type:'string', minLength:1, maxLength:500};
const operationFields = {service:textSchema, operation:textSchema, arguments:{type:'object'}, binding:textSchema};
const definitions = [
 ['blink_services','List reviewed and quarantined Web Link services.',{},[], 'services',true],
 ['blink_search','Search named operations with progressive disclosure.',{query:{type:'string',maxLength:500},service:textSchema,limit:{type:'integer',minimum:1,maximum:200},offset:{type:'integer',minimum:0,maximum:10000000}},[], 'search',true],
 ['blink_describe','Describe one operation and its reviewed execution contract.',{service:textSchema,operation:textSchema},['service','operation'],'describe',true],
 ['blink_call_read','Execute a reviewed read through the runtime grant and identity checks.',operationFields,['service','operation'],'call_read',true],
 ['blink_prepare','Prepare a non-read intent for separate human approval. Does not dispatch it.',{...operationFields,intentId:{type:'string',minLength:1,maxLength:100}},['service','operation'],'prepare',false],
 ['blink_execute','Execute one already-approved intent. This tool cannot issue approval.',{intentId:textSchema},['intentId'],'execute',false],
 ['blink_admin','Read runtime status or a redacted review; cannot authorize or configure account access.',{action:{type:'string',enum:['status','review','capabilities','sessions','jobs','job','binding','intent','workflow','canary']},id:textSchema,session:textSchema,job:textSchema,binding:textSchema,intentId:textSchema,runId:textSchema,candidate:textSchema},['action'],'admin',true],
 ['blink_doctor','Report host capabilities without needing Python, a daemon or browser.',{},[],'doctor',true],
 ['blink_request_approval','Carry out a host-confirmed action the user asked for. Local, reversible actions (learn_start/learn_stop recording, learn_propose, browser_open/browser_close, workflow_validate) run immediately. Actions with real effects (approve_intent for a prepared write, approve_manifest, bind, grant, jobs, revoke, forget, purge) show the human one Approve/Decline dialog with the exact parameters and run only if approved. Call it directly; do not ask for confirmation in chat first.',{method:{type:'string',minLength:1,maxLength:100},params:{type:'object'},reason:{type:'string',maxLength:500}},['method','params'],'approval',false],
];
const tools = definitions.map(([name,description,properties,required,method,readOnly]) => ({
 name,description,inputSchema:{type:'object',properties,required,additionalProperties:false},
 annotations:{readOnlyHint:readOnly,destructiveHint:name==='blink_execute',idempotentHint:readOnly,openWorldHint:true},
 ...(name==='blink_services'||name==='blink_search'||name==='blink_describe'?{_meta:{'anthropic/alwaysLoad':true}}:{}),
}));
const methods = new Map(definitions.map(d => [d[0], d[4]]));
let initialized = false, canElicit = false, nextRequest = 0;
const awaiting = new Map(), dialogs = new Map(), cancelled = new Set(); // dialog id -> resolver; tool call id -> dialog id
const APPROVAL_WAIT = 600000, DIALOG_LIMIT = 6000;

function binaryAvailable(command) {
 const result=spawnSync(command,['--version'],{stdio:'ignore',cwd:root,timeout:2000,windowsHide:true});
 return !result.error && result.status===0;
}
function launcher() {
 if(process.env.BLINK_PYTHON) return {command:process.env.BLINK_PYTHON,args:[],kind:'configured-python'};
 if(process.env.BLINK_DISABLE_UV!=='1' && binaryAvailable('uv'))
  return {command:'uv',args:['run','--offline','--quiet','--no-project','--with-requirements',path.join(root,'requirements.lock'),'python'],kind:'uv'};
 return {command:process.platform==='win32'?'python':'python3',args:[],kind:'python'};
}
function doctor() {
 const launch=launcher();
 const probeArgs=launch.args;
 const env={...process.env,PYTHONPATH:path.join(root,'src')};
 const probe=spawnSync(launch.command,[...probeArgs,'-c','import sys,json; import jsonschema; import blink.runtime; from blink.doctor import report; assert sys.version_info >= (3,11); print(json.dumps(report()))'],
  {env,cwd:root,encoding:'utf8',maxBuffer:65536,timeout:3500,windowsHide:true});
 let runtimeAvailable=false;
 if(!probe.error&&probe.status===0){try{runtimeAvailable=JSON.parse(probe.stdout).data.capabilities.runtime===true;}catch{}}
 const unix=process.platform!=='win32';
 return {status:runtimeAvailable && unix?'ok':'degraded',summary:'Host bootstrap capabilities',data:{
  capabilities:{diagnostics:true,runtime:runtimeAvailable&&unix,windowsRuntime:false},
  checks:{node:{available:true,version:process.versions.node},runtimeDependencies:{available:runtimeAvailable},
   launcher:{kind:launch.kind},unixTransport:{available:unix}},
  dependencyPolicy:'Doctor probes cached dependencies offline. Prepare pinned dependencies with the documented native setup command before runtime use; the MCP launcher stays offline.'},
  warnings:unix?[]:['Windows supports diagnostics; the current local runtime requires POSIX Unix sockets.']};
}
async function doctorWithDaemon(){
 // Ask the daemon too: its handshake replaces an outdated one, and a version mismatch is reported here.
 const local=doctor();
 if(!local.data.capabilities.runtime)return local;
 const remote=await backend('doctor',{});
 if(!['ok','degraded'].includes(remote?.status)){
  local.status='degraded';local.data.capabilities.runtime=false;local.data.daemonError={status:remote?.status??'unknown',summary:remote?.summary??''};
  local.warnings.push('The local daemon is not reachable: '+(remote?.summary??'no answer'));return local;
 }
 local.data.daemon=remote?.data?.daemon??null;
 local.data.client=remote?.data?.client??null;
 local.data.daemonMatchesClient=remote?.data?.daemonMatchesClient===true;
 if(!local.data.daemonMatchesClient)local.warnings.push('The local daemon does not match this plugin version: '+JSON.stringify(local.data.daemon?.version??'unknown')+' vs '+JSON.stringify(local.data.client?.version??'unknown')+'. Stop any recording, then call blink_doctor again to restart it.');
 for(const w of remote?.warnings??[])local.warnings.push(w);
 return local;
}
function backend(method,params,module=['blink.client'],timeout=45000) {
 return new Promise(resolve=>{
  const launch=launcher();
  const args=[...launch.args,'-m',...module,'--plugin-root',root];
  if(process.env.BLINK_STATE_DIR) args.push('--state-dir',process.env.BLINK_STATE_DIR);
  const env={...process.env,PYTHONPATH:path.join(root,'src')};
  let child;
  const failed=()=>resolve({status:'env_unavailable',summary:'Runtime is unavailable; run blink_doctor and the human setup command.',warnings:[]});
  try {child=spawn(launch.command,args,{env,cwd:root,stdio:['pipe','pipe','ignore'],windowsHide:true});}catch {failed();return;}
  let output=[],outputBytes=0,done=false;
  const finish=value=>{if(!done){done=true;clearTimeout(timer);resolve(value);}};
  const timer=setTimeout(()=>{child.kill();finish({status:'env_unavailable',summary:'Runtime request timed out; an already dispatched intent must be reconciled.',warnings:[]});},timeout);
  child.on('error',()=>finish({status:'env_unavailable',summary:'Python runtime launcher is unavailable.',warnings:[]}));
  child.stdout.on('data',data=>{
   output.push(data);outputBytes+=data.length;
   if(outputBytes>LIMIT){child.kill();finish({status:'response_too_large',summary:'Runtime response limit exceeded.',warnings:[]});}
  });
  child.on('close',code=>{
   if(done)return;
   try{
    if(code!==0)throw new Error();
    const result=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(Buffer.concat(output)));
    if(!result || typeof result!=='object' || Array.isArray(result) || typeof result.status!=='string')throw new Error();
    finish(result);
   }catch{finish({status:'env_unavailable',summary:'Runtime did not return a valid response.',warnings:[]});}
  });
  child.stdin.on('error',()=>{});
  child.stdin.end(JSON.stringify({method,params})+'\n');
 });
}
function elicit(message,requestedSchema,callId){
 return new Promise(resolve=>{
  const id=`blink-approval-${++nextRequest}`;
  const timer=setTimeout(()=>{awaiting.delete(id);dialogs.delete(callId);resolve({action:'cancel'});},APPROVAL_WAIT);
  dialogs.set(callId,id);
  awaiting.set(id,reply=>{clearTimeout(timer);dialogs.delete(callId);resolve(reply);});
  send({jsonrpc:'2.0',id,method:'elicitation/create',params:{message,requestedSchema}});
 });
}
function approvalText(d,reason){
 const lines=[d.text];
 // One line, bounded: a model-written reason must not imitate the dialog's own lines.
 if(reason)lines.push('',`Claude's stated reason (not verified): ${reason.replace(/\s+/g,' ').slice(0,300)}`);
 lines.push('','Only approve if this is what you want. Claude cannot approve this for you.');
 return lines.join('\n');
}
async function approval(args,callId){
 if(!/^[a-z][a-z0-9_]{0,99}$/.test(args.method))return {status:'forbidden',summary:'This action cannot be approved in the dialog',warnings:[]};
 const request={method:args.method,params:args.params};
 const fallback={method:args.method,command:`blink human ${args.method} --input <file with the parameters JSON>`};
 const described=await backend('describe',request,['blink.approval','describe']);
 if(described.status!=='ok')return described;
 const apply=()=>cancelled.has(callId)?{status:'declined',summary:'The request was cancelled; nothing was changed',warnings:[]}
  :backend('apply',{...request,digest:described.data.exactParametersSha256},['blink.approval','apply'],APPROVAL_WAIT);
 // Local, reversible actions the user asked for run at once; only actions with real effects get a dialog.
 if(described.data.immediate===true)return apply();
 if(!canElicit)return {status:'approval_unavailable',summary:'This client cannot show an approval dialog; the human must run the CLI command',data:fallback,warnings:[]};
 if(cancelled.has(callId))return {status:'declined',summary:'The request was cancelled; nothing was changed',warnings:[]};
 const text=approvalText(described.data,args.reason);
 if(text.length>DIALOG_LIMIT)return {status:'approval_too_large',summary:'This action is too large to review in a dialog; the human must use the CLI command',data:fallback,warnings:[]};
 const answer=await elicit(text,{type:'object',properties:{decision:{type:'string',title:'Decision',enum:['approve','decline'],enumNames:['Approve','Decline']}},required:['decision']},callId);
 if(answer?.action!=='accept'||answer?.content?.decision!=='approve')
  return {status:'declined',summary:'The human did not approve this action; nothing was changed',data:{method:args.method},warnings:[]};
 return apply();
}
function rpcError(id,code,message){return {jsonrpc:'2.0',id,error:{code,message}};}
function validArguments(tool,args){
 if(!args||typeof args!=='object'||Array.isArray(args))return false;
 const schema=tool.inputSchema;
 if(Object.keys(args).some(k=>!Object.hasOwn(schema.properties,k))||schema.required.some(k=>!(k in args)))return false;
 for(const [k,v]of Object.entries(args)){
  const p=schema.properties[k];
  if(p.type==='string'&&(typeof v!=='string'||v.length<(p.minLength||0)||v.length>(p.maxLength||100000)))return false;
  if(p.enum&&!p.enum.includes(v))return false;
  if(p.type==='object'&&(!v||typeof v!=='object'||Array.isArray(v)))return false;
  if(p.type==='number'&&(typeof v!=='number'||!Number.isFinite(v)||v<(p.minimum??-Infinity)))return false;
  if(p.type==='integer'&&(!Number.isInteger(v)||v<(p.minimum??-Infinity)||v>(p.maximum??Infinity)))return false;
 }
 return true;
}
async function handle(message){
 if(!message||typeof message!=='object'||Array.isArray(message)||message.jsonrpc!=='2.0'||typeof message.method!=='string')return rpcError(null,-32600,'Invalid Request');
 const hasId=Object.hasOwn(message,'id');
 if(hasId && (message.id===null || !['string','number'].includes(typeof message.id) || (typeof message.id==='number'&&!Number.isFinite(message.id))))return rpcError(null,-32600,'Invalid request id');
 if(!hasId)return null; // Notifications never produce responses or perform tool actions.
 const id=message.id;
 if(Object.hasOwn(message,'params')&&(!message.params||typeof message.params!=='object'||Array.isArray(message.params)))return rpcError(id,-32602,'Invalid params');
 if(message.method==='initialize'){
  const p=message.params;
  if(!p||typeof p!=='object'||typeof p.protocolVersion!=='string'||!p.capabilities||typeof p.capabilities!=='object'||Array.isArray(p.capabilities)||!p.clientInfo||typeof p.clientInfo!=='object'||typeof p.clientInfo.name!=='string'||typeof p.clientInfo.version!=='string')return rpcError(id,-32602,'Invalid initialize parameters');
  initialized=true;
  const e=p.capabilities.elicitation; // form mode: {form:{}}, or the legacy empty object
  canElicit=!!e&&typeof e==='object'&&!Array.isArray(e)&&(Object.keys(e).length===0||!!e.form);
  return {jsonrpc:'2.0',id,result:{protocolVersion:PROTOCOLS.includes(p.protocolVersion)?p.protocolVersion:PROTOCOLS[0],capabilities:{tools:{listChanged:false}},serverInfo:{name:'web-blink',version:'0.2.7'},instructions:'Act on what the user asks without asking again in chat. Use blink_request_approval for host-confirmed actions: recording and other local steps run at once, actions with real effects get one Approve/Decline dialog for the human. Tell the user what you started. While recording, the browser belongs to the user: do not act in it unless they ask, and ask them how to proceed instead of rushing ahead. Never ask the human to type shell commands unless the tool reports approval_unavailable.'}};
 }
 if(message.method==='ping')return {jsonrpc:'2.0',id,result:{}};
 if(!initialized)return rpcError(id,-32002,'Server is not initialized');
 if(message.method==='tools/list'){
  if(message.params?.cursor)return rpcError(id,-32602,'Unknown pagination cursor');
  return {jsonrpc:'2.0',id,result:{tools}};
 }
 if(message.method==='tools/call'){
  const p=message.params;
  if(!p||typeof p!=='object'||typeof p.name!=='string')return rpcError(id,-32602,'Invalid tool call');
  const tool=tools.find(t=>t.name===p.name);
  if(!tool)return rpcError(id,-32602,'Unknown tool');
  const args=p.arguments??{};
  if(!validArguments(tool,args))return rpcError(id,-32602,'Invalid tool arguments');
  const result=p.name==='blink_doctor'?await doctorWithDaemon():p.name==='blink_request_approval'?await approval(args,id):await backend(methods.get(p.name),args);
  return {jsonrpc:'2.0',id,result:{content:[{type:'text',text:JSON.stringify(result)}],isError:!['ok','degraded','prepared','quarantined','approved','succeeded','resolved_done','declined','approval_unavailable','approval_too_large'].includes(result.status)}};
 }
 return rpcError(id,-32601,'Method not found');
}
function send(value){if(value!==null)process.stdout.write(JSON.stringify(value)+'\n');}
let pending='',dropping=false,queue=Promise.resolve();
const decoder=new StringDecoder('utf8');
process.stdin.on('data',chunk=>{
 const data=decoder.write(chunk);
 pending+=data;
 while(true){
  const end=pending.indexOf('\n');
  if(end<0)break;
  const line=pending.slice(0,end);pending=pending.slice(end+1);
  if(dropping){dropping=false;continue;}
  if(Buffer.byteLength(line)>LIMIT){send(rpcError(null,-32700,'Message exceeds transport limit'));continue;}
  let message;
  try{message=JSON.parse(line);}catch{queue=queue.then(()=>send(rpcError(null,-32700,'Parse error')));continue;}
  if(message&&typeof message==='object'&&!Array.isArray(message)&&!Object.hasOwn(message,'method')&&awaiting.has(message.id)){
   // A client answer to our own approval dialog: never queued behind the tool call that waits for it.
   const done=awaiting.get(message.id);awaiting.delete(message.id);done(message.error?{action:'cancel'}:message.result);continue;
  }
  if(message?.method==='notifications/cancelled'&&['string','number'].includes(typeof message.params?.requestId)){
   // A cancelled tool call must not stay approvable, whether its dialog is open, pending or not yet shown.
   const call=message.params.requestId;
   if(cancelled.size>=1000)cancelled.clear();
   cancelled.add(call);
   const dialog=dialogs.get(call);const done=awaiting.get(dialog);awaiting.delete(dialog);done?.({action:'cancel'});continue;
  }
  queue=queue.then(async()=>{
   try{send(await handle(message));}catch{send(rpcError(message?.id??null,-32603,'Internal error'));}
  });
 }
 if(Buffer.byteLength(pending)>LIMIT){pending='';dropping=true;send(rpcError(null,-32700,'Message exceeds transport limit'));}
});
process.stdin.on('end',()=>{if(pending.trim()&&!dropping)send(rpcError(null,-32700,'Incomplete message'));});
