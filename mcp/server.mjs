#!/usr/bin/env node
/** Claude-compatible MCP stdio server. Diagnostics require only this Node file. */
import {spawn, spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import {StringDecoder} from 'node:string_decoder';
import fs from 'node:fs';
import os from 'node:os';
import {randomBytes} from 'node:crypto';

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
 ['blink_admin','Read runtime status, local profile names and aliases from the automatically refreshed metadata index (action browsers; optional browser filter and refresh), recording status, sessions, a review, or the user\'s private Blink. No credential databases are indexed.',{action:{type:'string',enum:['status','browsers','recording','review','capabilities','sessions','jobs','job','binding','intent','workflow','canary','private']},browser:{type:'string',enum:['chrome','edge','brave','firefox']},refresh:{type:'boolean'},id:textSchema,service:textSchema,blink:textSchema,session:textSchema,job:textSchema,binding:textSchema,intentId:textSchema,runId:textSchema,candidate:textSchema,offset:{type:'integer',minimum:0,maximum:1000000},limit:{type:'integer',minimum:1,maximum:200}},['action'],'admin',true],
 ['blink_doctor','Report host capabilities without needing Python, a daemon or browser.',{},[],'doctor',true],
 ['blink_connect_browser','Open/connect a browser only; this does NOT start recording. For a learn/record request use blink_start_recording instead. After connection explain guidance and ask its next-step question, or continue an already-authorized recording in the same turn. Never stop at “you can log in now”.', {browser:{type:'object'},binding:textSchema,waitSeconds:{type:'integer',minimum:0,maximum:30}},['browser'],'connect_browser',false],
 ['blink_start_recording','Connect the chosen browser AND start recording the requested app in one guided action. Use for learn/record requests, including after switching to a new browser. Preserve the app URL through pending browser consent; follow nextActions to finish. Only recording:true means recording started.',{url:{type:'string',minLength:1,maxLength:2048},browser:{type:'object'},binding:{...textSchema,maxLength:100},waitSeconds:{type:'integer',minimum:0,maximum:30}},['url','browser'],'start_recording',false],
 ['blink_browser_status','Read actual recording state and the last verified browser connection without opening tabs, reconnecting or running OS commands. Use for “is it recording?”; pass binding for a previously known/chosen recording, including after host restart. HTTP 404 alone does not prove native connection failure. Follow guidance; never run shell port/process probes.',{browser:{type:'object'},binding:{...textSchema,maxLength:100}},[],'browser_status',true],
 ['blink_restore_browser','Ask the browser to reopen one selected profile and restore its saved session. Closes no windows, terminates no processes and does not claim restored tabs were verified.',{browser:{type:'object'}},['browser'],'restore_browser',false],
 ['blink_request_approval','Carry out the exact action the user ordered. The user\'s order is the authorization: the action runs at once (recording, activation, bind, grants, approve_intent for a prepared write, jobs, forget, restart). Call it directly in the same turn; never ask for confirmation in chat and never announce a dialog. Only when the user set approvals to dialog in the CLI does the client show one Accept/Decline dialog.',{method:{type:'string',minLength:1,maxLength:100},params:{type:'object'},reason:{type:'string',maxLength:500}},['method','params'],'approval',false],
];
const tools = definitions.map(([name,description,properties,required,method,readOnly]) => ({
 name,description,inputSchema:{type:'object',properties,required,additionalProperties:false},
 ...(name==='blink_connect_browser'?{title:'Connect a browser profile'}:name==='blink_restore_browser'?{title:'Reopen a browser profile'}:name==='blink_start_recording'?{title:'Start recording an app'}:name==='blink_browser_status'?{title:'Check browser and recording status'}:{}),
 annotations:{readOnlyHint:readOnly,destructiveHint:name==='blink_execute',idempotentHint:readOnly,openWorldHint:true},
 ...(name==='blink_services'||name==='blink_search'||name==='blink_describe'?{_meta:{'anthropic/alwaysLoad':true}}:{}),
}));
const methods = new Map(definitions.map(d => [d[0], d[4]]));
const browserFlowId=randomBytes(16).toString('hex'); // Conversation routing, not an authorization credential.
let initialized = false, canElicit = false, nextRequest = 0;
const awaiting = new Map(), dialogs = new Map(), cancelled = new Set(); // dialog id -> resolver; tool call id -> dialog id
const APPROVAL_WAIT = 600000, DIALOG_LIMIT = 6000;
// An unanswered dialog counts as declined; BLINK_DIALOG_WAIT_MS can only shorten the wait (tests).
const DIALOG_WAIT = Math.min(APPROVAL_WAIT, Number(process.env.BLINK_DIALOG_WAIT_MS) > 0 ? Number(process.env.BLINK_DIALOG_WAIT_MS) : APPROVAL_WAIT);

function binaryAvailable(command) {
 const result=spawnSync(command,['--version'],{stdio:'ignore',cwd:root,timeout:2000,windowsHide:true});
 return !result.error && result.status===0;
}
function runtimePaths(){
 const home=path.resolve(process.env.BLINK_HOME||path.join(os.homedir(),'.web-blink'));
 const directory=path.join(home,'runtime');
 return {directory,cache:path.join(directory,'uv-cache'),python:path.join(directory,'python'),marker:path.join(directory,'ready.json'),uv:path.join(directory,'bin',process.platform==='win32'?'uv.exe':'uv')};
}
function preparedRuntime(){
 try{const p=runtimePaths(),info=fs.lstatSync(p.marker);
  if(!info.isFile()||info.isSymbolicLink()||info.size>1024)return false;
  const marker=JSON.parse(fs.readFileSync(p.marker,'utf8'));
  return marker.schemaVersion===1&&marker.cache==='app-local';
 }catch{return false;}
}
function launcher() {
 if(process.env.BLINK_PYTHON&&!preparedRuntime()) return {command:process.env.BLINK_PYTHON,args:[],kind:'configured-python'};
 const prepared=preparedRuntime(),p=runtimePaths(),uv=prepared&&fs.existsSync(p.uv)?p.uv:'uv';
 if(process.env.BLINK_DISABLE_UV!=='1' && binaryAvailable(uv)){
  return {command:uv,args:[...(prepared?['--cache-dir',p.cache]:[]),'run','--offline','--quiet','--no-project','--with-requirements',path.join(root,'requirements.lock'),'python'],
   kind:prepared?'uv-managed':'uv',...(prepared?{env:{UV_PYTHON_INSTALL_DIR:p.python}}:{})};
 }
 return {command:process.platform==='win32'?'python':'python3',args:[],kind:'python'};
}
function doctor() {
 const launch=launcher();
 const probeArgs=launch.args;
 const env={...process.env,...(launch.env||{}),PYTHONPATH:path.join(root,'src'),PYTHONUTF8:'1',PYTHONIOENCODING:'utf-8'};
 const probe=spawnSync(launch.command,[...probeArgs,'-c','import sys,json; import jsonschema; import blink.runtime; from blink.doctor import report; assert sys.version_info >= (3,11); print(json.dumps(report()))'],
  {env,cwd:root,encoding:'utf8',maxBuffer:65536,timeout:3500,windowsHide:true});
 let runtimeAvailable=false;
 let homeDir=null,moving=[];
 if(!probe.error&&probe.status===0){try{const report=JSON.parse(probe.stdout),data=report.data;runtimeAvailable=data.capabilities.runtime===true;homeDir=data.homeDir??null;
  moving=(report.warnings??[]).filter(w=>typeof w==='string'&&w.startsWith('Web Blink data stays'));}catch{}}
 const unix=process.platform!=='win32';
 const setup={tool:'blink_request_approval',method:'runtime_setup',params:{},
  title:'Prepare Blink on this computer',
  description:'Download Python and the required libraries once into Blink\'s own data folder. Does not open a browser or read browser logins.',
  writesTo:runtimePaths().directory,
  hostPermission:'The host may ask to authorize this preparation tool. Explain that it downloads local components, not access to browser logins. Do not change host permission or sandbox settings.'};
 return {status:runtimeAvailable?'ok':'degraded',summary:'Host bootstrap capabilities',data:{
  capabilities:{diagnostics:true,browserDiscovery:true,runtime:runtimeAvailable,windowsRuntime:!unix&&runtimeAvailable},
  activeComponent:{version:'0.11.0',pluginRoot:root},
  checks:{node:{available:true,version:process.versions.node},runtimeDependencies:{available:runtimeAvailable},
   launcher:{kind:launch.kind},unixTransport:{available:unix},localTransport:{available:true,kind:unix?'unix-socket':'named-pipe'}},homeDir,
  ...(runtimeAvailable?{}:{runtimeSetup:setup}),
  dependencyPolicy:'Ordinary startup checks components offline. A user-ordered browser action can prepare missing components in Blink’s private folder; no session hook or shell instructions are needed.'},
  warnings:[...(runtimeAvailable?[]:['Blink needs one-time preparation. Explain data.runtimeSetup.description and its hostPermission before requesting this native tool action. Windows profile discovery is already available.']),...moving]};
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
  // The approval policy is read where config.py reads it: BLINK_CONFIG_DIR, else the Web Blink home.
  if(module[0]==='blink.approval'&&process.env.BLINK_CONFIG_DIR) args.push('--config-dir',process.env.BLINK_CONFIG_DIR);
  const env={...process.env,...(launch.env||{}),PYTHONPATH:path.join(root,'src'),PYTHONUTF8:'1',PYTHONIOENCODING:'utf-8'};
  let child;
  const failed=()=>resolve({status:'env_unavailable',summary:'Blink could not start its local components. Use its native preparation action, then continue the requested connection.',warnings:[]});
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
   }catch{finish({status:'env_unavailable',summary:'Blink could not start its local components. Prepare them with the runtime_setup tool action, then retry the original request.',warnings:[]});}
  });
  child.stdin.on('error',()=>{});
  child.stdin.end(JSON.stringify({method,params})+'\n');
 });
}
function elicit(message,requestedSchema,callId){
 return new Promise(resolve=>{
  const id=`blink-approval-${++nextRequest}`;
  const timer=setTimeout(()=>{awaiting.delete(id);dialogs.delete(callId);resolve({action:'cancel'});},DIALOG_WAIT);
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
 if(!/^[a-z][a-z0-9_]{0,99}$/.test(args.method))return {status:'forbidden',summary:'This action is not supported by the Web Blink control bridge',warnings:[]};
 if(['browser_connect','learn_start_guided','learn_start','learn_stop'].includes(args.method))
  args={...args,params:{...args.params,_flowId:browserFlowId}};
 if(args.method==='runtime_setup')return prepareRuntime(args.params,callId);
 if(args.method==='browser_restart')return {status:'restart_not_isolated',summary:'Blink cannot restart just this profile without risking other profiles in the shared browser instance. No windows were closed. Use live connection or the optional PW extension; use safe reopen for recovery.',data:{closedWindows:false,nextActions:[
  {tool:'blink_connect_browser',params:{browser:args.params?.browser},label:'Connect without closing any windows'},
  {tool:'blink_restore_browser',params:{browser:args.params?.browser},label:'For recovery: reopen the selected profile and request saved-session restore; close nothing'}]},warnings:[]};
 if(['learn_start','learn_start_guided','browser_connect','browser_restore','browser_select','browser_permission_open','browser_extension_open','browser_setup'].includes(args.method)
    && !doctor().data.capabilities.runtime){
  const ready=await prepareRuntime({},callId);if(ready.status!=='ok')return ready;
 }
 const request={method:args.method,params:args.params};
 // Validation, the exact-parameter digest and the user's approval policy come from the local runtime and its
 // trusted settings file; never from tool arguments.
 const described=await backend('describe',request,['blink.approval','describe']);
 if(described.status!=='ok')return described;
 const declined={status:'declined',summary:'The request was cancelled; nothing was changed',warnings:[]};
 const apply=()=>cancelled.has(callId)?declined
  :backend('apply',{...request,digest:described.data.exactParametersSha256},['blink.approval','apply'],APPROVAL_WAIT);
 if(cancelled.has(callId))return declined;
 // Default policy: the user's order authorizes the exact described action; no dialog, whatever the client supports.
 if(described.data.mode==='none')return apply();
 if(described.data.mode!=='dialog')return {status:'configuration_error',summary:'Unknown approval policy; nothing was changed',warnings:[]};
 // Opt-in dialog policy (the user set approvals to dialog). Local, reversible actions still run at once.
 const fallback={method:args.method,command:`blink human ${args.method} --input <file with the parameters JSON>`};
 if(described.data.immediate===true)return apply();
 if(!canElicit)return {status:'approval_unavailable',summary:'Approval dialogs are switched on, but this client cannot show one; the human must run the CLI command',data:fallback,warnings:[]};
 const text=approvalText(described.data,args.reason);
 if(text.length>DIALOG_LIMIT)return {status:'approval_too_large',summary:'This action is too large to review in a dialog; the human must use the CLI command',data:fallback,warnings:[]};
 // One click: Accept approves, Decline or Esc does not (no form fields: a field made Accept count as decline).
 const answer=await elicit(text+'\n\nAccept = approve and run it. Decline = do nothing.',{type:'object',properties:{}},callId);
 if(answer?.action!=='accept')
  return {status:'declined',summary:'The human did not approve this action; nothing was changed',data:{method:args.method},warnings:[]};
 return apply();
}
async function prepareRuntime(params,callId){
 if(!params||typeof params!=='object'||Array.isArray(params)||Object.keys(params).length)
  return {status:'invalid_arguments',summary:'Runtime preparation takes no configuration or account data.',warnings:[]};
 let policy='none';
 try{
  const config=path.join(process.env.BLINK_CONFIG_DIR||path.join(path.resolve(process.env.BLINK_HOME||path.join(os.homedir(),'.web-blink')),'config'),'settings.json');
  if(fs.existsSync(config)){
   for(let current=path.dirname(config);;current=path.dirname(current)){
    if(fs.existsSync(current)&&(fs.lstatSync(current).isSymbolicLink()||fs.existsSync(path.join(current,'.git'))))throw Error();
    if(path.dirname(current)===current)break;
   }
   const info=fs.lstatSync(config);if(!info.isFile()||info.isSymbolicLink()||info.size>131072)throw Error();
   if(process.platform!=='win32'&&((info.mode&0o077)||info.uid!==process.getuid()))throw Error();
   const document=JSON.parse(fs.readFileSync(config,'utf8'));if(document.schemaVersion!==1||!document.defaults||!document.blinks)throw Error();policy=document.approvals??'none';
   if(!['none','dialog'].includes(policy))throw Error();
  }
 }catch{return {status:'configuration_error',summary:'Blink cannot read its approval preference. Preparation has not started.',warnings:[]};}
 if(policy==='dialog'){
  if(!canElicit)return {status:'approval_unavailable',summary:'Your Blink preference requires a confirmation dialog, but this host cannot display it. Preparation has not started.',warnings:[]};
  const answer=await elicit('Prepare Blink on this computer? This downloads Python and required libraries into '+runtimePaths().directory+
   '. It does not open a browser or read browser logins. Accept prepares it once; Decline leaves it unchanged.',{type:'object',properties:{}},callId);
  if(answer?.action!=='accept')return {status:'declined',summary:'Blink preparation was declined.',warnings:[]};
 }
 if(cancelled.has(callId))return {status:'declined',summary:'Blink preparation was cancelled before starting.',warnings:[]};
 return new Promise(resolve=>{
  let child,output='',done=false;
  const finish=result=>{if(!done){done=true;clearTimeout(timer);resolve(result);}};
  const timer=setTimeout(()=>{child?.kill();finish({status:'env_unavailable',summary:'Blink preparation timed out. Retry preparation; no browser has been opened.',warnings:[]});},270000);
  try{child=spawn(process.execPath,[path.join(root,'mcp/setup-runtime.mjs'),'--json'],{cwd:root,env:process.env,windowsHide:true,stdio:['ignore','pipe','ignore']});}
  catch{finish({status:'env_unavailable',summary:'Blink could not start its preparation component.',warnings:[]});return;}
  child.on('error',()=>finish({status:'env_unavailable',summary:'Blink could not start its preparation component.',warnings:[]}));
  child.stdout.on('data',data=>{output+=data.toString('utf8');if(Buffer.byteLength(output)>65536){child.kill();finish({status:'env_unavailable',summary:'Blink preparation returned an invalid result.',warnings:[]});}});
  child.on('close',()=>{try{const result=JSON.parse(output);if(typeof result.status!=='string')throw Error();finish(result);}
   catch{finish({status:'env_unavailable',summary:'Blink preparation did not complete. The browser and its logins were not accessed.',warnings:[]});}});
 });
}
function rpcError(id,code,message){return {jsonrpc:'2.0',id,error:{code,message}};}
function toolFailure(message,error){
 const tool=tools.find(t=>t.name===message?.params?.name);
 if(message?.method!=='tools/call'||!tool)return rpcError(message?.id??null,-32603,'Internal error');
 const kinds=['TypeError','RangeError','SyntaxError','ReferenceError','Error'];
 const operation=message.params?.arguments?.method??message.params?.arguments?.action??methods.get(tool.name);
 return {jsonrpc:'2.0',id:message.id,result:{isError:true,content:[{type:'text',text:JSON.stringify({
  status:'internal_error',summary:'Blink could not finish this tool action. Inspect its current state before retrying an action that may have changed anything.',
  data:{tool:tool.name,operation:typeof operation==='string'&&/^[a-z_]{1,100}$/.test(operation)?operation:'tool_action',
        errorType:kinds.includes(error?.name)?error.name:'Error'},warnings:[]})}]}};
}
function validArguments(tool,args){
 if(!args||typeof args!=='object'||Array.isArray(args))return false;
 const schema=tool.inputSchema;
 if(Object.keys(args).some(k=>!Object.hasOwn(schema.properties,k))||schema.required.some(k=>!(k in args)))return false;
 for(const [k,v]of Object.entries(args)){
  const p=schema.properties[k];
  if(p.type==='string'&&(typeof v!=='string'||v.length<(p.minLength||0)||v.length>(p.maxLength||100000)))return false;
  if(p.enum&&!p.enum.includes(v))return false;
  if(p.type==='object'&&(!v||typeof v!=='object'||Array.isArray(v)))return false;
  if(p.type==='boolean'&&typeof v!=='boolean')return false;
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
  return {jsonrpc:'2.0',id,result:{protocolVersion:PROTOCOLS.includes(p.protocolVersion)?p.protocolVersion:PROTOCOLS[0],capabilities:{tools:{listChanged:false}},serverInfo:{name:'web-blink',version:'0.11.0'},instructions:'The user\'s order authorizes Web Blink. Execute the requested chain (prepare, authorize the exact plan, execute; record, propose, activate) in the same turn without asking for approval in chat or announcing dialogs. Never change the approval setting (approvals is the user\'s, set only by them in the CLI). Ask only for missing information required to perform the task. Tell the user what you started. While recording, the browser belongs to the user: do not act in it unless they ask. For every handoff state whether recording is ON/OFF and follow guidance with one next action or a plain next-step question in the user’s language. For learn/record goals use blink_start_recording to connect AND record; browser changes preserve that original goal. Never stop at browser connected or answer only “No, I only opened it”. Use blink_browser_status for diagnostics; do not run shell HTTP, port-owner or process commands, and never infer failure from HTTP 404 alone. For browser choice, show data.profileTable and accept #numbers. Prepare local components through the native tool action; never hand the user setup commands or copied profile paths. Follow nextActions and explain the exact browser confirmation. Discovery alone is not connection. Shared-profile browser restart is blocked. Use the narrow blink_connect_browser tool for ordinary connection; proactively offer safe alternatives if native access fails. For waiting_for_browser, explain userSteps and immediately call its bounded wait nextAction while the user clicks, at most twice; never ask for done or claim a connection before connected:true. For recovery use blink_restore_browser, which closes nothing and reports restore requests honestly.'}};
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
  const result=p.name==='blink_doctor'?await doctorWithDaemon():p.name==='blink_request_approval'?await approval(args,id)
   :p.name==='blink_connect_browser'?await approval({method:'browser_connect',params:{...args,automatic:true}},id)
   :p.name==='blink_start_recording'?await approval({method:'learn_start_guided',params:args},id)
   :p.name==='blink_browser_status'?await backend('admin',{action:'browser_connection',...args,_flowId:browserFlowId})
   :p.name==='blink_restore_browser'?await approval({method:'browser_restore',params:args},id)
   :process.platform==='win32'&&p.name==='blink_admin'&&args.action==='browsers'
    ?{status:'ok',summary:'Local profile names and aliases (no Python or daemon required)',data:(await import('./browsers.mjs')).discoverWindows(args.browser,{refresh:args.refresh===true}),warnings:[]}
    :await backend(methods.get(p.name),args);
  return {jsonrpc:'2.0',id,result:{content:[{type:'text',text:JSON.stringify(result)}],isError:!['ok','degraded','prepared','quarantined','approved','succeeded','resolved_done','declined','approval_unavailable','approval_too_large','waiting_for_browser'].includes(result.status)}};
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
   try{send(await handle(message));}catch(error){send(toolFailure(message,error));}
  });
 }
 if(Buffer.byteLength(pending)>LIMIT){pending='';dropping=true;send(rpcError(null,-32700,'Message exceeds transport limit'));}
});
process.stdin.on('end',()=>{if(pending.trim()&&!dropping)send(rpcError(null,-32700,'Incomplete message'));});
