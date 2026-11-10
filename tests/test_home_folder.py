import errno
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from blink import home
from blink.config import ConfigStore, default_directory
from blink.doctor import default_state_dir, report, secure_state_directory
from blink.storage import Store

CLEARED = ('BLINK_HOME', 'BLINK_STATE_DIR', 'BLINK_CONFIG_DIR', 'XDG_STATE_HOME', 'XDG_CONFIG_HOME',
           'APPDATA', 'LOCALAPPDATA')


@unittest.skipIf(os.name == 'nt', 'POSIX permission and lock checks')
class HomeFolderTests(unittest.TestCase):
    """Every test runs in a temporary HOME: the real ~/.web-blink and old folders are never touched."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        environment = {key: value for key, value in os.environ.items() if key not in CLEARED}
        environment['HOME'] = str(self.root)
        patcher = patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertEqual(Path.home(), self.root)
        self.home = self.root / '.web-blink'
        self.old_state, self.old_config = home.legacy_dirs()
        self.assertTrue(self.old_state.is_relative_to(self.root))

    def legacy(self):
        self.old_state.mkdir(parents=True, mode=0o700)
        os.chmod(self.old_state, 0o700)
        store = Store(self.old_state)
        folder = self.old_state / 'captures' / 'example-20260101T000000Z'
        (folder / 'screenshots').mkdir(parents=True, mode=0o700)
        os.chmod(folder, 0o700)
        (folder / 'screenshots' / '0001.jpg').write_bytes(b'jpeg')
        (folder / 'meta.json').write_text(json.dumps({'id': folder.name, 'folder': str(folder),
                                                      'files': {'shots': str(folder / 'screenshots')}}))
        (folder / 'index.json').write_text(json.dumps({'id': folder.name, 'folder': str(folder)}))
        store.put('captures', folder.name, {'id': folder.name, 'status': 'stopped', 'folder': str(folder)})
        store.put('captures', 'elsewhere', {'id': 'elsewhere', 'status': 'stopped', 'folder': '/srv/other'})
        (self.old_state / 'runtime.lock').write_bytes(b'')
        (self.old_state / 'secrets.json').write_text('{"s": 1}')
        ConfigStore(self.old_config).set('output', 'table')
        self.settings = ConfigStore(self.old_config).load()
        return folder.name

    def test_default_layout_is_one_private_home_folder(self):
        self.assertEqual(default_state_dir(), self.home / 'state')
        self.assertEqual(default_directory(), self.home / 'config')
        for path in (self.home, self.home / 'private'):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertEqual(list((self.home / 'private').iterdir()), [])
        self.assertEqual(secure_state_directory(), self.home / 'state')
        self.assertEqual(stat.S_IMODE((self.home / 'state').stat().st_mode), 0o700)
        self.assertEqual(Store().path, self.home / 'state')
        ConfigStore().set('output', 'table')
        self.assertTrue((self.home / 'config' / 'settings.json').is_file())
        data = report()['data']
        self.assertEqual(data['homeDir'], str(self.home))
        self.assertEqual(data['stateDir'], str(self.home / 'state'))
        self.assertEqual(data['privateDir'], str(self.home / 'private'))
        self.assertFalse(self.old_state.exists())

    def test_blink_home_override_and_explicit_directories(self):
        self.legacy()
        custom = self.root / 'custom-home'
        with patch.dict(os.environ, {'BLINK_HOME': str(custom)}):
            self.assertEqual(default_state_dir(), custom / 'state')
            self.assertEqual(default_directory(), custom / 'config')
            self.assertEqual(stat.S_IMODE((custom / 'private').stat().st_mode), 0o700)
            self.assertEqual(report()['data']['homeDir'], str(custom))
        explicit = self.root / 'explicit'
        with patch.dict(os.environ, {'BLINK_STATE_DIR': str(explicit), 'BLINK_CONFIG_DIR': str(self.root / 'cfg')}):
            self.assertEqual(default_state_dir(), explicit)
            self.assertEqual(default_directory(), self.root / 'cfg')
            report()
        # An explicit home or explicit directories never pull the old default folders in.
        self.assertTrue((self.old_state / 'blink.sqlite3').is_file())
        self.assertTrue((self.old_config / 'settings.json').is_file())
        self.assertFalse((self.old_state / home.NOTE).exists())

    def test_migration_moves_state_and_config_and_rewrites_capture_paths(self):
        session = self.legacy()
        old_folder = self.old_state / 'captures' / session
        state = default_state_dir()
        self.assertEqual(state, self.home / 'state')
        folder = state / 'captures' / session
        self.assertEqual((folder / 'screenshots' / '0001.jpg').read_bytes(), b'jpeg')
        self.assertEqual(json.loads((folder / 'meta.json').read_text())['folder'], str(folder))
        self.assertEqual(json.loads((folder / 'meta.json').read_text())['files']['shots'], str(folder / 'screenshots'))
        self.assertEqual(json.loads((folder / 'index.json').read_text())['folder'], str(folder))
        store = Store(state)
        self.assertEqual(store.get('captures', session)['folder'], str(folder))
        self.assertEqual(store.get('captures', 'elsewhere')['folder'], '/srv/other')
        self.assertEqual((state / 'secrets.json').read_text(), '{"s": 1}')
        self.assertTrue((state / 'runtime.lock').exists())
        self.assertFalse(old_folder.exists())
        self.assertEqual(os.listdir(self.old_state), [home.NOTE])
        self.assertIn(str(state), (self.old_state / home.NOTE).read_text())
        # Config moves on its first use; state was moved alone.
        self.assertTrue((self.old_config / 'settings.json').exists())
        self.assertEqual(default_directory(), self.home / 'config')
        self.assertEqual(ConfigStore().load(), self.settings)
        self.assertEqual(os.listdir(self.old_config), [home.NOTE])
        note = (self.old_state / home.NOTE).read_text()
        before = sorted(str(p.relative_to(self.home)) for p in self.home.rglob('*'))
        self.assertEqual(home.migrate(), {'status': 'none'})
        self.assertEqual(default_state_dir(), state)
        self.assertEqual((self.old_state / home.NOTE).read_text(), note)
        self.assertEqual(sorted(str(p.relative_to(self.home)) for p in self.home.rglob('*')), before)

    def test_no_move_while_old_daemon_holds_lock(self):
        import fcntl
        self.legacy()
        fd = os.open(self.old_state / 'runtime.lock', os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(home.migrate()['status'], 'deferred')
            self.assertEqual(default_state_dir(), self.old_state)
            self.assertEqual(default_directory(), self.old_config)
            self.assertIn('old folder', ' '.join(report()['warnings']))
            self.assertFalse((self.home / 'state').exists())
            self.assertFalse((self.old_state / home.NOTE).exists())
            self.assertTrue((self.old_state / 'blink.sqlite3').is_file())
        finally:
            os.close(fd)
        self.assertEqual(default_state_dir(), self.home / 'state')
        self.assertTrue((self.home / 'state' / 'blink.sqlite3').is_file())

    def test_never_overwrites_the_new_folder(self):
        self.legacy()
        (self.home / 'config').mkdir(parents=True, mode=0o700)
        (self.home / 'config' / 'settings.json').write_text('{"new": true}')
        self.assertEqual(default_directory(), self.home / 'config')
        self.assertEqual((self.home / 'config' / 'settings.json').read_text(), '{"new": true}')
        self.assertTrue((self.old_config / 'settings.json').is_file())
        # An interrupted move resumes into a non-empty folder but still skips names already there.
        (self.home / 'state').mkdir(mode=0o700)
        (self.home / 'state' / 'secrets.json').write_text('{"kept": 1}')
        (self.old_state / home.NOTE).write_text(home.MOVING + ' this folder.\n')
        self.assertEqual(default_state_dir(), self.home / 'state')
        self.assertEqual((self.home / 'state' / 'secrets.json').read_text(), '{"kept": 1}')
        self.assertEqual(sorted(os.listdir(self.old_state)), sorted([home.NOTE, 'secrets.json']))
        self.assertIn('secrets.json', (self.old_state / home.NOTE).read_text())
        self.assertTrue((self.home / 'state' / 'blink.sqlite3').is_file())
        self.assertEqual(home.migrate(), {'status': 'none'})

    def test_other_filesystem_copies_verifies_and_removes(self):
        session = self.legacy()
        real = os.rename

        def rename(source, target):
            if str(source).startswith(str(self.old_state)):
                raise OSError(errno.EXDEV, 'cross-device link')
            return real(source, target)
        with patch('blink.home.os.rename', rename):
            state = default_state_dir()
        folder = state / 'captures' / session
        self.assertEqual((folder / 'screenshots' / '0001.jpg').read_bytes(), b'jpeg')
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(Store(state).get('captures', session)['folder'], str(folder))
        self.assertEqual(os.listdir(self.old_state), [home.NOTE])


if __name__ == '__main__':
    unittest.main()
