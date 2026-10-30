"""Private SQLite state and process-safe credential-family fencing."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading

from .errors import BlinkError


def strict_json(text):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise BlinkError('invalid_json', 'Duplicate JSON object keys are forbidden')
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError):
        raise BlinkError('invalid_json', 'Invalid strict JSON') from None


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def private_dir(path):
    original = Path(path).expanduser().absolute()
    for part in [original, *original.parents]:
        if part.is_symlink():
            raise BlinkError('unsafe_state', 'State paths must not traverse symlinks')
    resolved = original.resolve()
    for parent in [resolved, *resolved.parents]:
        if (parent / '.git').exists():
            raise BlinkError('unsafe_state', 'State must be outside Git worktrees')
    resolved.mkdir(parents=True, mode=0o700, exist_ok=True)
    st = resolved.stat()
    if not stat.S_ISDIR(st.st_mode) or (hasattr(os, 'getuid') and st.st_uid != os.getuid()):
        raise BlinkError('unsafe_state', 'State directory is not owned by the current user')
    if os.name != 'nt' and stat.S_IMODE(st.st_mode) & 0o077:
        raise BlinkError('unsafe_state', 'State directories require mode 0700')
    return resolved


def private_file(path):
    path = Path(path)
    if path.is_symlink():
        raise BlinkError('unsafe_state', 'State files cannot be symlinks')
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(fd)
    st = path.stat()
    if not stat.S_ISREG(st.st_mode) or (hasattr(os, 'getuid') and st.st_uid != os.getuid()):
        raise BlinkError('unsafe_state', 'State file ownership/type mismatch')
    if os.name != 'nt' and stat.S_IMODE(st.st_mode) & 0o077:
        raise BlinkError('unsafe_state', 'State files require mode 0600')
    return path


class Store:
    def __init__(self, state_dir=None):
        selected = state_dir or os.environ.get('BLINK_STATE_DIR')
        if not selected:
            from .doctor import default_state_dir
            selected = default_state_dir()
        self.path = private_dir(selected)
        self.state_dir = self.path
        self.db_path = private_file(self.path / 'blink.sqlite3')
        self._lock = threading.RLock()
        self._local = threading.local()
        with self.transaction() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS objects(kind TEXT NOT NULL,id TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS intents(
              id TEXT PRIMARY KEY,service TEXT NOT NULL,operation TEXT NOT NULL,binding TEXT,digest TEXT NOT NULL,
              args_json TEXT NOT NULL,arguments_hash TEXT NOT NULL,effect TEXT NOT NULL,state TEXT NOT NULL,
              created REAL NOT NULL,expires REAL NOT NULL,approval_id TEXT,idempotency_key TEXT NOT NULL,
              amount REAL NOT NULL DEFAULT 0,currency TEXT,result_json TEXT,outcome TEXT);
            CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY,intent_id TEXT NOT NULL,plan_digest TEXT NOT NULL,
              expires REAL NOT NULL,state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reservations(intent_id TEXT PRIMARY KEY,binding TEXT,amount REAL NOT NULL,
              currency TEXT,state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,at REAL NOT NULL,event TEXT NOT NULL,
              reference TEXT,status TEXT NOT NULL);
            ''')

    def connect(self):
        private_dir(self.path)
        private_file(self.db_path)
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=30000')
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        return db

    @contextlib.contextmanager
    def transaction(self):
        with self._lock:
            nested = getattr(self._local, 'connection', None)
            if nested is not None:
                yield nested
                return
            db = self.connect()
            self._local.connection = db
            try:
                db.execute('BEGIN IMMEDIATE')
                yield db
                if db.in_transaction:
                    db.execute('COMMIT')
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise
            finally:
                self._local.connection = None
                db.close()

    def get(self, kind, key, default=None, *, connection=None):
        if connection is None:
            with self.transaction() as db:
                return self.get(kind, key, default, connection=db)
        row = connection.execute('SELECT value FROM objects WHERE kind=? AND id=?', (kind, key)).fetchone()
        return strict_json(row[0]) if row else default

    def put(self, kind, key, value, *, connection=None):
        if connection is None:
            with self.transaction() as db:
                return self.put(kind, key, value, connection=db)
        connection.execute('INSERT INTO objects(kind,id,value) VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET value=excluded.value',
                           (kind, key, encoded(value)))
        return value

    def delete(self, kind, key, *, connection=None):
        if connection is None:
            with self.transaction() as db:
                return self.delete(kind, key, connection=db)
        connection.execute('DELETE FROM objects WHERE kind=? AND id=?', (kind, key))

    def list(self, kind, *, connection=None):
        if connection is None:
            with self.transaction() as db:
                return self.list(kind, connection=db)
        return [strict_json(row[0]) for row in connection.execute('SELECT value FROM objects WHERE kind=? ORDER BY id', (kind,))]

    @contextlib.contextmanager
    def family_lock(self, family):
        path = private_file(self.path / ('family-' + hashlib.sha256(family.encode()).hexdigest() + '.lock'))
        with self._lock, path.open('r+b') as handle:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b'0'); handle.flush()
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == 'nt':
                    handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
