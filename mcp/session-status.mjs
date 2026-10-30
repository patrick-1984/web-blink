// Advisory only: no browser, installer, runtime startup or authorizing action.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
const root=path.dirname(path.dirname(fileURLToPath(import.meta.url)));
if(process.platform==='win32') {
  process.stdout.write('Web Blink: diagnostics available; current runtime transport requires POSIX.\n');
} else {
  // ~/.web-blink/state; the old platform folder only until the runtime has moved it (see blink/home.py).
  const home=process.env.BLINK_HOME || path.join(os.homedir(),'.web-blink');
  const legacy=process.platform==='darwin'
    ? path.join(os.homedir(),'Library/Application Support/WebBlink')
    : path.join(process.env.XDG_STATE_HOME || path.join(os.homedir(),'.local/state'),'web-blink');
  const state=process.env.BLINK_STATE_DIR || (process.env.BLINK_HOME || fs.existsSync(path.join(home,'state','blink.sqlite3'))
    || !fs.existsSync(path.join(legacy,'blink.sqlite3')) ? path.join(home,'state') : legacy);
  try {
    const absolute=path.resolve(state),db=path.join(absolute,'blink.sqlite3');
    if(!fs.existsSync(db)||fs.realpathSync(absolute)!==absolute||fs.lstatSync(db).isSymbolicLink())process.exit(0);
    const dir=fs.statSync(absolute),info=fs.statSync(db);
    if((dir.mode&0o077)||(info.mode&0o077)||dir.uid!==os.userInfo().uid||info.uid!==os.userInfo().uid)process.exit(0);
    const code='import sqlite3,sys,pathlib,json; p=pathlib.Path(sys.argv[1]); db=sqlite3.connect(p.as_uri()+"?mode=ro",uri=True,timeout=0.1); rows=db.execute("SELECT value FROM objects WHERE kind=?",("notifications",)); print(sum(json.loads(r[0]).get("state")=="unread" for r in rows))';
    const env={...process.env};delete env.PYTHONPATH;
    const result=spawnSync(process.env.BLINK_PYTHON||'python3',['-I','-c',code,db],
      {env,cwd:root,encoding:'utf8',stdio:['ignore','pipe','ignore'],timeout:800,maxBuffer:1024,windowsHide:true});
    const count=Number(result.stdout?.trim());
    if(result.status===0&&Number.isSafeInteger(count)&&count>0)
      process.stdout.write(`Web Blink: ${count} unread account/job notice(s); run blink status.\n`);
  } catch {}
}
