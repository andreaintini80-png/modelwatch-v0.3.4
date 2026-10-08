"""Optional Redis REST storage, stdlib only; atomic state per source.

Local processing still uses pathlib/atomic writes. A remote session implements
the small file interface needed by that same algorithm, entirely in memory,
then commits baseline, pending and history together with compare-and-set.
"""
import json
import os
import re
import urllib.request
from urllib.parse import urlparse


class StorageError(ValueError):
    pass


def hosted():
    mode = os.environ.get('MODELWATCH_STORAGE', 'local')
    if mode not in ('local', 'redis-rest'):
        raise StorageError('Invalid MODELWATCH_STORAGE')
    return mode == 'redis-rest'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise StorageError('Remote storage redirect rejected')


class RedisREST:
    def __init__(self):
        self._url = os.environ.get('UPSTASH_REDIS_REST_URL', '')
        self._token = os.environ.get('UPSTASH_REDIS_REST_TOKEN', '')
        self.prefix = os.environ.get('MODELWATCH_REDIS_PREFIX', '')
        p = urlparse(self._url)
        if (p.scheme != 'https' or not p.hostname or p.username or p.password
                or p.query or p.fragment or p.path not in ('', '/')
                or not self._token or '\n' in self._token or '\r' in self._token
                or not re.fullmatch(r'[A-Za-z0-9:_-]{1,128}', self.prefix)):
            raise StorageError('Remote storage environment configuration is invalid')
        # Prefix is mandatory: installations must never share a default namespace.
        self._url = self._url.rstrip('/')

    def command(self, *args):
        try:
            # Never put credentials or state in URLs; never forward auth on redirects.
            from .core import validate_public_https
            # Upstash's provider-owned hostnames also work behind HTTPS proxies
            # where target DNS is performed by the proxy, not the client.
            # Other Redis REST destinations retain public-address DNS checks.
            host = urlparse(self._url).hostname or ''
            provider_host = bool(re.fullmatch(r'[a-zA-Z0-9-]+\.upstash\.io', host))
            validate_public_https(self._url, resolve_host=not provider_host)
            req = urllib.request.Request(self._url,
                data=json.dumps(list(args)).encode('utf-8'),
                headers={'Authorization': 'Bearer ' + self._token,
                         'Content-Type': 'application/json', 'User-Agent': 'codex-modelwatch/0.3.4'},
                method='POST')
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as r:
                raw = r.read(20_000_001)
                if len(raw) > 20_000_000:
                    raise StorageError('Remote state exceeds safety limit')
                data = json.loads(raw)
            if not isinstance(data, dict) or 'error' in data or 'result' not in data:
                raise StorageError('Remote storage command rejected')
            return data['result']
        except Exception:
            # Provider bodies, exception text, endpoint and auth never escape.
            raise StorageError('Remote storage unavailable or request rejected') from None

    def session(self, source_key):
        return RemoteSession(self, self.prefix + ':source:' + source_key)


class RemotePath:
    def __init__(self, session, key):
        self.session, self.key = session, key

    @property
    def parent(self):
        return RemotePath(self.session, self.key.rsplit('/', 1)[0])

    def __truediv__(self, name):
        return RemotePath(self.session, self.key + '/' + str(name))

    def mkdir(self, **kwargs):
        pass

    def exists(self):
        return self.key in self.session.files

    def read_text(self, **kwargs):
        if not self.exists():
            raise FileNotFoundError('Remote state entry missing')
        return self.session.files[self.key]

    def write_atomic(self, text):
        self.session.files[self.key] = text

    def unlink(self, missing_ok=False):
        if not self.exists() and not missing_ok:
            raise FileNotFoundError('Remote state entry missing')
        self.session.files.pop(self.key, None)


class RemoteSession:
    def __init__(self, client, key):
        self.client, self.key = client, key
        self.original = client.command('GET', key)
        try:
            if self.original is None:
                self.files = {}
            else:
                obj = json.loads(self.original)
                if (not isinstance(obj, dict) or obj.get('schema') != 1
                        or not isinstance(obj.get('files'), dict)
                        or not all(isinstance(k, str) and isinstance(v, str)
                                   for k, v in obj['files'].items())):
                    raise ValueError()
                self.files = obj['files']
        except Exception:
            raise StorageError('Remote state is corrupt; no changes applied') from None

    def path(self, key):
        return RemotePath(self, key)

    def commit(self):
        value = json.dumps({'schema': 1, 'files': self.files}, ensure_ascii=False, sort_keys=True)
        if value == self.original or (self.original is None and not self.files):
            return
        # One atomic write; conflict fails closed rather than losing observations.
        script = """local old=redis.call('GET',KEYS[1])
if (ARGV[1]=='missing' and old) or (ARGV[1]=='present' and old~=ARGV[2]) then return 0 end
redis.call('SET',KEYS[1],ARGV[3]); return 1"""
        result = self.client.command('EVAL', script, 1, self.key,
            'missing' if self.original is None else 'present', self.original or '', value)
        if result != 1:
            raise StorageError('Remote state changed concurrently; retry the check')
