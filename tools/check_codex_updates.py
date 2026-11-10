"""Exercise real Codex plugin updates in an isolated child home against synthetic local Git repos.

No model session, user credentials, real plugin installation or browser tools are invoked.
"""
import http.server
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from urllib.parse import urlsplit


def main():
    with tempfile.TemporaryDirectory(prefix='blink-codex-update-check-') as scratch:
        root = Path(scratch)
        git_env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
                       GIT_AUTHOR_NAME='Update fixture', GIT_AUTHOR_EMAIL='fixture@example.invalid',
                       GIT_COMMITTER_NAME='Update fixture', GIT_COMMITTER_EMAIL='fixture@example.invalid')
        def git(*args, cwd=root):
            return subprocess.check_output(['git', *args], cwd=cwd, env=git_env, stderr=subprocess.DEVNULL, text=True).strip()
        for name in ('plugin', 'marketplace'):
            git('init', '--bare', str(root / (name + '.git')))
            git('init', '-b', 'plugin' if name == 'plugin' else 'main', str(root / name))
            git('remote', 'add', 'origin', str(root / (name + '.git')), cwd=root / name)

        class GitHTTP(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                self.handle_git()
            def do_POST(self):
                self.handle_git()
            def handle_git(self):
                url = urlsplit(self.path)
                if not url.path.startswith(('/plugin.git/', '/marketplace.git/')) or '..' in url.path.split('/'):
                    self.send_error(404); return
                length = int(self.headers.get('Content-Length', '0'))
                if length > 10 * 1024 * 1024:
                    self.send_error(413); return
                env = dict(git_env, GIT_PROJECT_ROOT=str(root), GIT_HTTP_EXPORT_ALL='1', PATH_INFO=url.path,
                           QUERY_STRING=url.query, REQUEST_METHOD=self.command,
                           CONTENT_TYPE=self.headers.get('Content-Type', ''), CONTENT_LENGTH=str(length),
                           SERVER_PROTOCOL='HTTP/1.1', REMOTE_ADDR='127.0.0.1')
                proc = subprocess.run(['git', 'http-backend'], input=self.rfile.read(length),
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30)
                header, separator, body = proc.stdout.partition(b'\r\n\r\n')
                if not separator:
                    self.send_error(502); return
                status, headers = 200, []
                for line in header.decode('latin-1').splitlines():
                    key, _, value = line.partition(':')
                    if key.lower() == 'status': status = int(value.strip().split()[0])
                    else: headers.append((key, value.strip()))
                self.send_response(status)
                for key, value in headers: self.send_header(key, value)
                self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), GitHTTP)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            origin = f'http://127.0.0.1:{server.server_port}'
            plugin, catalog = root / 'plugin', root / 'marketplace'
            (plugin / '.codex-plugin').mkdir()
            (plugin / 'skills/fixture').mkdir(parents=True)
            (plugin / 'skills/fixture/SKILL.md').write_text('---\nname: fixture\ndescription: Synthetic update fixture.\n---\nDo not run anything.\n')
            def plugin_version(version):
                (plugin / '.codex-plugin/plugin.json').write_text(json.dumps({
                    'name': 'update-fixture', 'version': version, 'description': 'Synthetic update fixture', 'skills': './skills/'}))
                git('add', '.', cwd=plugin); git('commit', '-m', version, cwd=plugin); git('push', 'origin', 'plugin', cwd=plugin)
            (catalog / '.claude-plugin').mkdir()
            def catalog_ref(ref, version=None):
                entry = {'name': 'update-fixture', 'source': {'source': 'url', 'url': origin + '/plugin.git', 'ref': ref}}
                if version: entry['version'] = version
                (catalog / '.claude-plugin/marketplace.json').write_text(json.dumps({
                    'name': 'update-check', 'owner': {'name': 'Fixture'}, 'plugins': [entry]}))
                git('add', '.', cwd=catalog); git('commit', '-m', 'Catalog ' + ref, cwd=catalog); git('push', 'origin', 'main', cwd=catalog)
            plugin_version('0.9.1'); catalog_ref('plugin')
            fixture_home = root / 'codex-fixture'
            fixture_home.mkdir(mode=0o700)
            # CODEX_HOME is configured only for these child CLI processes, for its intended isolated app state.
            env = dict(git_env, CODEX_HOME=str(fixture_home))
            def codex(*args):
                proc = subprocess.run(['codex', 'plugin', *args, '--json'], env=env, cwd=root,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8', timeout=90)
                if proc.returncode:
                    raise RuntimeError(proc.stderr or proc.stdout)
                return json.loads(proc.stdout)
            added = codex('marketplace', 'add', origin + '/marketplace.git', '--ref', 'main')
            assert Path(added['installedRoot']).resolve().is_relative_to(fixture_home.resolve()), added
            initial = codex('add', 'update-fixture@update-check')
            location = Path(initial['installedPath'])
            assert location.resolve().is_relative_to(fixture_home.resolve()), initial
            def installed_versions():
                return sorted(json.loads(p.read_text())['version'] for p in location.parent.glob('*/.codex-plugin/plugin.json'))
            before = installed_versions()
            plugin_version('0.10.0')
            codex('marketplace', 'upgrade', 'update-check')
            moving_after_upgrade = installed_versions()
            explicit = codex('add', 'update-fixture@update-check')
            assert json.loads((Path(explicit['installedPath']) / '.codex-plugin/plugin.json').read_text())['version'] == '0.10.0'
            plugin_version('0.10.1'); git('tag', 'v0.10.1', cwd=plugin); git('push', 'origin', 'v0.10.1', cwd=plugin)
            catalog_ref('v0.10.1', '0.10.1')
            codex('marketplace', 'upgrade', 'update-check')
            pinned_after_upgrade = installed_versions()
            pinned = codex('add', 'update-fixture@update-check')
            assert json.loads((Path(pinned['installedPath']) / '.codex-plugin/plugin.json').read_text())['version'] == '0.10.1'
            print(json.dumps({'codexVersion': subprocess.check_output(['codex', '--version'], text=True).strip(),
                'initial': before, 'movingRefAfterMarketplaceUpgrade': moving_after_upgrade,
                'movingRefExplicitAdd': '0.10.0', 'pinnedRefAfterMarketplaceUpgrade': pinned_after_upgrade,
                'pinnedRefExplicitAdd': '0.10.1', 'isolated': True}))
        finally:
            server.shutdown(); server.server_close()


if __name__ == '__main__':
    main()
