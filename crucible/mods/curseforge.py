"""Official CurseForge API adapter. Never invent CDN URLs or bypass distribution rules."""
from __future__ import annotations
import json
import os
import stat
from pathlib import Path
import urllib.request
import urllib.parse
import urllib.error
from . import modrinth as mr

_API = 'https://api.curseforge.com/v1'
_session_key = ''
_DEFAULT_KEY_FILE = Path.home() / '.config/crucible/curseforge-api-key'
_LOADERS = {'forge': 1, 'fabric': 4, 'quilt': 5, 'neoforge': 6}

# Conservative release fallback: keep the adapter available for future review,
# but do not use the CurseForge API/CDN until written app-specific authorization
# and data-retention guidance have been reviewed and accepted.
_CURSEFORGE_ONLINE_ENABLED = False
_DISABLED_MESSAGE = "CurseForge online support is temporarily disabled while Crucible's API authorization and data-retention requirements remain unresolved. Use Modrinth, or obtain a permitted JAR independently and add it with Add .jar. Existing local mods and records are not changed."


def online_integration_enabled() -> bool:
    return _CURSEFORGE_ONLINE_ENABLED


def ensure_online_integration_enabled() -> None:
    if not online_integration_enabled():
        raise mr.ModrinthError(_DISABLED_MESSAGE)

def set_api_key(key: str):
    global _session_key
    _session_key = key.strip()

def get_api_key():
    """Read only user-owned private key files. Never log credential contents."""
    key = _session_key or os.environ.get('CURSEFORGE_API_KEY', '').strip()
    if not key:
        path = Path(os.environ.get('CURSEFORGE_API_KEY_FILE') or _DEFAULT_KEY_FILE).expanduser()
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return ''
        except OSError as exc:
            raise mr.ModrinthError('Could not open the private CurseForge key file. Use a regular file owned by your user, not a symlink.') from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise mr.ModrinthError('The CurseForge key file must be owned by your user and private (chmod 600).')
            if info.st_size > 8192:
                raise mr.ModrinthError('The CurseForge key file is too large; it must contain only the API key.')
            with os.fdopen(fd, 'rb', closefd=False) as source:
                raw = source.read(8193)
            key = raw.decode('utf-8').strip()
        except UnicodeError as exc:
            raise mr.ModrinthError('The CurseForge key file must contain plain UTF-8 text.') from exc
        finally:
            os.close(fd)
    if len(key) > 8192 or '\n' in key or '\r' in key:
        raise mr.ModrinthError('The CurseForge key must be a single line, not a configuration document.')
    return key


def has_api_key():
    # Do not inspect a locally configured key while online support is disabled.
    return online_integration_enabled() and bool(get_api_key())

def _get(route):
    ensure_online_integration_enabled()
    key = get_api_key()
    if not key:
        raise mr.ModrinthError('CurseForge needs your own approved API key. Use the API key button (session only), place it in ~/.config/crucible/curseforge-api-key (chmod 600), set CURSEFORGE_API_KEY before launching, or download a JAR from curseforge.com and use Add .jar. Restricted downloads are never bypassed.')
    from ..download_http import make_request, open_url, http_error_message
    request = make_request(_API + route, headers={'Accept': 'application/json', 'User-Agent': mr._UA})
    try:
        with open_url(request, timeout=15) as response:
            raw = response.read(mr._MAX_JSON_BYTES + 1)
        if len(raw) > mr._MAX_JSON_BYTES:
            raise mr.ModrinthError('CurseForge response exceeded the size limit.')
        return json.loads(raw)['data']
    except urllib.error.HTTPError as exc:
        raise mr.ModrinthError(http_error_message(exc.code, curseforge=True)) from exc
    except (OSError, ValueError, KeyError) as exc:
        raise mr.ModrinthError('Could not read the CurseForge response. Check your connection and try again.') from exc

def search(query='', *, loader='', mc_version='', limit=30, offset=0):
    args = {'gameId':432, 'classId':6, 'searchFilter':query, 'pageSize':min(50,limit), 'index':offset, 'sortField':2, 'sortOrder':'desc'}
    if loader in _LOADERS: args['modLoaderType'] = _LOADERS[loader]
    if mc_version: args['gameVersion'] = mc_version
    return [mr.ModHit(project_id=f"cf:{m['id']}", slug=m.get('slug',''), title=m.get('name','?'), description=m.get('summary',''), downloads=int(m.get('downloadCount',0)), icon_url=(m.get('logo') or {}).get('thumbnailUrl',''), author=', '.join(a.get('name','') for a in m.get('authors',[]))) for m in _get('/mods/search?' + urllib.parse.urlencode(args))]

def _file(pid, data):
    sha1 = next((h['value'] for h in data.get('hashes',[]) if h.get('algo') == 1), '')
    return mr.ModFile(project_id=f'cf:{pid}', title=data.get('displayName',''), version_id=str(data['id']), version_number=data.get('displayName',''), filename=data['fileName'], url=data.get('downloadUrl') or '', sha1=sha1, size=int(data.get('fileLength',0)), dependencies=[f"cf:{d['modId']}" for d in data.get('dependencies',[]) if d.get('relationType') == 3])

def list_versions(project_id, *, loader, mc_version):
    ensure_online_integration_enabled()
    if not mc_version or loader not in _LOADERS:
        raise mr.ModrinthError('Set the Minecraft version and a supported mod loader in Setup first.')
    pid = project_id.removeprefix('cf:')
    if not pid.isdigit(): raise mr.ModrinthError('Invalid CurseForge project ID.')
    files=[]
    for offset in range(0, 1000, 50):
        args=urllib.parse.urlencode({'gameVersion':mc_version,'modLoaderType':_LOADERS[loader],'pageSize':50,'index':offset})
        page=_get(f'/mods/{pid}/files?{args}')
        files.extend(_file(pid,v) for v in page if v.get('isAvailable',True))
        if len(page)<50: break
    return files

def resolve_with_deps(project_id, *, loader, mc_version, _seen=None):
    seen = set() if _seen is None else _seen
    if project_id in seen: return []
    seen.add(project_id)
    versions=list_versions(project_id,loader=loader,mc_version=mc_version)
    if not versions: raise mr.ModrinthError('No compatible CurseForge file was found.')
    main=versions[0]
    if not main.url: raise mr.ModrinthError('The author restricts direct downloads. Open this project on CurseForge, download the file, then use Add .jar.')
    out=[main]
    for dep in main.dependencies: out.extend(resolve_with_deps(dep,loader=loader,mc_version=mc_version,_seen=seen))
    return out
