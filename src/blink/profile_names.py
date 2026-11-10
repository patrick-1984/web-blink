"""Human profile labels and a bounded metadata-only discovery cache."""
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import unicodedata


def normalize(value):
    return re.sub(r'[\W_]+', ' ', unicodedata.normalize('NFKC', str(value)).casefold()).strip()


def _text(value):
    return ''.join(c for c in value if c.isprintable()).strip()[:150] if isinstance(value, str) else ''


def chromium_names(info, preferences, directory):
    """Read only explicitly named profile/account label fields; never persist Preferences itself."""
    info = info if isinstance(info, dict) else {}
    profile = preferences.get('profile') if isinstance(preferences, dict) else {}
    profile = profile if isinstance(profile, dict) else {}
    sources = []
    def add(source, value):
        value = _text(value)
        if value and not any(x['value'] == value for x in sources):
            sources.append({'source': source, 'value': value})
        return value
    local = add('Local State.enterprise_label', info.get('enterprise_label'))
    cached = add('Local State.name', info.get('name'))
    pref = add('Preferences.profile.name', profile.get('name'))
    shortcut = add('Local State.shortcut_name', info.get('shortcut_name')) or add('Preferences.profile.shortcut_name', profile.get('shortcut_name'))
    gaia = add('Local State.gaia_given_name', info.get('gaia_given_name')) or add('Local State.gaia_name', info.get('gaia_name'))
    for section in ('account_info', 'edge_account_info', 'edge_account', 'edge_profile'):
        values = preferences.get(section) if isinstance(preferences, dict) else None
        values = values if isinstance(values, list) else [values]
        for entry in values[:10]:
            if isinstance(entry, dict):
                for key in ('profile_name', 'display_name', 'full_name', 'given_name', 'name'):
                    label = add(f'Preferences.{section}.{key}', entry.get(key))
                    if not gaia and key in ('given_name', 'full_name', 'display_name'):
                        gaia = label
    generic = lambda x: not x or bool(re.fullmatch(r'(?:default|profile|person|profil|osoba)(?:\s*\d+)?', x, re.I))
    local = local or next((x for x in (pref, cached) if not generic(x)), cached or pref or directory)
    aliases = [entry['value'] for entry in sources]
    combined = gaia and normalize(gaia) != normalize(local) and not generic(local) and not normalize(local).startswith(normalize(gaia) + ' ')
    if combined:
        aliases += [f'{gaia} ({local})', f'{gaia} - {local}', f'{gaia} – {local}']
    name = shortcut if shortcut and not generic(shortcut) else (
        f'{gaia} ({local})' if combined
        else gaia if gaia and (generic(local) or info.get('is_using_default_name') is True) else local)
    return {'name': name, 'displayName': name, 'aliases': list(dict.fromkeys([name, *aliases, directory])),
            'nameSources': sources, 'directoryName': directory}


def firefox_groups(root):
    found = []
    group = root / 'Profile Groups'
    try:
        databases = sorted(group.glob('*.sqlite'))[:100]
    except OSError:
        return found
    for database in databases:
        if database.is_symlink():
            continue
        try:
            connection = sqlite3.connect(database.absolute().as_uri() + '?mode=ro', uri=True, timeout=.1)
            try:
                connection.execute('PRAGMA query_only=ON')
                for relative, name in connection.execute('SELECT path,name FROM Profiles LIMIT 1000'):
                    if not isinstance(relative, str) or not _text(name):
                        continue
                    path = Path(relative)
                    path = path if path.is_absolute() else root / path
                    if path.is_dir() and not path.is_symlink():
                        found.append((path.absolute(), _text(name), str(database)))
            finally:
                connection.close()
        except (OSError, sqlite3.Error, ValueError):
            continue
    return found


def stamps(paths):
    result = []
    for path in sorted(set(map(str, paths))):
        try:
            stat = Path(path).stat()
            result.append([path, str(stat.st_mtime_ns), stat.st_size])
        except OSError:
            result.append([path, None, None])
    return result


class ProfileIndex:
    def __init__(self, home, environment, platform):
        self.directory = Path(environment.get('BLINK_HOME', Path(home) / '.web-blink')) / 'cache'
        self.path = self.directory / 'browser-profiles.json'
        self.platform = platform
        self.document = {'schemaVersion': 2, 'platform': platform, 'entries': {}}
        try:
            if not self.path.is_symlink() and self.path.stat().st_size <= 4 * 1024 * 1024:
                value = json.loads(self.path.read_text(encoding='utf-8'))
                if isinstance(value, dict) and value.get('schemaVersion') == 2 and value.get('platform') == platform and isinstance(value.get('entries'), dict):
                    self.document = value
        except (OSError, ValueError, TypeError):
            pass
        numbers = self.document.get('numbers', {})
        self.document['numbers'] = numbers if isinstance(numbers, dict) and all(type(v) is int and v > 0 for v in numbers.values()) else {}
        self.document['nextNumber'] = max([0, *self.document['numbers'].values()]) + 1
        self.changed = False
        self.updated = set()

    def number(self, browser):
        for profile in browser.get('profiles', []):
            numbers = self.document['numbers']
            if profile['id'] not in numbers:
                numbers[profile['id']] = self.document['nextNumber']
                self.document['nextNumber'] += 1
                self.changed = True
            profile['number'] = numbers[profile['id']]
            profile['reference'] = '#' + str(profile['number'])
        return browser

    def get(self, kind, refresh=False):
        entry = self.document['entries'].get(kind)
        if (refresh or not isinstance(entry, dict) or not isinstance(entry.get('scannedAt'), (int, float))
                or time.time() - entry.get('scannedAt', 0) > 300
                or not isinstance(entry.get('fingerprint'), list)):
            return None
        try:
            unchanged = stamps([s[0] for s in entry['fingerprint']]) == entry['fingerprint']
        except (TypeError, IndexError):
            unchanged = False
        if not unchanged:
            return None
        browser = entry.get('browser')
        if not isinstance(browser, dict) or not isinstance(browser.get('profiles'), list) or any(not isinstance(p, dict) or not isinstance(p.get('id'), str) for p in browser['profiles']):
            return None
        return self.number(browser)

    def put(self, kind, browser, paths):
        self.document['entries'][kind] = {'scannedAt': time.time(), 'fingerprint': stamps(paths), 'browser': self.number(browser)}
        self.changed = True
        self.updated.add(kind)

    def save(self):
        if not self.changed:
            return 'cached'
        temporary = None
        locked = False
        lock = self.directory / '.profile-index.lock'
        try:
            if any(p.is_symlink() or (p / '.git').exists() for p in (self.directory, *self.directory.parents)):
                return 'memory-only'
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name == 'nt':
                from .windows import protect
                protect(self.directory)
            else:
                self.directory.chmod(0o700)
            if self.path.is_symlink():
                return 'memory-only'
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    lock.mkdir(mode=0o700)
                    locked = True
                    break
                except FileExistsError:
                    try:
                        if time.time() - lock.stat().st_mtime > 300:
                            lock.rmdir()
                    except OSError:
                        pass
                    time.sleep(.01)
            if not locked:
                return 'memory-only'
            latest = {}
            try:
                if self.path.stat().st_size <= 4 * 1024 * 1024:
                    latest = json.loads(self.path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                pass
            if isinstance(latest, dict) and latest.get('schemaVersion') == 2 and latest.get('platform') == self.platform:
                numbers = latest.get('numbers', {})
                if isinstance(numbers, dict) and all(type(v) is int and v > 0 for v in numbers.values()):
                    self.document['numbers'] = numbers
                    self.document['nextNumber'] = max([0, *numbers.values()]) + 1
                merged = latest.get('entries', {})
                if isinstance(merged, dict):
                    for kind, entry in self.document['entries'].items():
                        if kind in self.updated or kind not in merged:
                            merged[kind] = entry
                        if isinstance(entry, dict) and isinstance(entry.get('browser'), dict):
                            self.number(entry['browser'])
                    self.document['entries'] = merged
                    for entry in merged.values():
                        if isinstance(entry, dict) and isinstance(entry.get('browser'), dict):
                            self.number(entry['browser'])
            fd, temporary = tempfile.mkstemp(prefix='.profiles-', dir=self.directory)
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(self.document, handle, ensure_ascii=False)
                handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            return 'refreshed'
        except (OSError, ValueError):
            return 'memory-only'
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
            if locked:
                lock.rmdir()


def profile_table(browsers):
    clean = lambda value: str(value).replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')
    rows = [{'id': p['reference'], 'browser': b['browser'], 'profile': p.get('displayName', p['name']),
             'profileId': p['id'], 'directory': p['directory']} for b in browsers for p in b.get('profiles', [])]
    rows.sort(key=lambda row: int(row['id'][1:]))
    table = '| ID | Browser | Profile |\n|---|---|---|\n' + '\n'.join(
        '| ' + ' | '.join(clean(row[key]) for key in ('id', 'browser', 'profile')) + ' |' for row in rows)
    return rows, table
