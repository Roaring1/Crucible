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
    return bool(get_api_key())


def _is_fingerprint_whitespace(byte):
    return byte in (9, 10, 13, 32)


def fingerprint_bytes(data: bytes) -> int:
    """Compute CurseForge's file fingerprint (Murmur2 variant, seed 1).

    CurseForge normalizes by removing TAB, LF, CR and ASCII space before
    hashing. This is not SHA-1 and must only be used with its fingerprint API.
    """
    mask = 0xFFFFFFFF
    multiplier = 1540483477
    length = sum(not _is_fingerprint_whitespace(b) for b in data)
    h = 1 ^ length
    word = 0
    shift = 0
    for byte in data:
        if _is_fingerprint_whitespace(byte):
            continue
        word |= byte << shift
        shift += 8
        if shift == 32:
            mixed = (word * multiplier) & mask
            mixed = (((mixed ^ (mixed >> 24)) * multiplier) & mask)
            h = ((h * multiplier) ^ mixed) & mask
            word = 0
            shift = 0
    if shift:
        h = ((h ^ word) * multiplier) & mask
    mixed = ((h ^ (h >> 13)) * multiplier) & mask
    return (mixed ^ (mixed >> 15)) & mask


def fingerprint_file(path, *, chunk_size=1024 * 1024) -> int:
    """Fingerprint a file in two bounded-memory passes, including large JARs."""
    path = Path(path)
    normalized_length = 0
    with path.open('rb') as source:
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            normalized_length += sum(not _is_fingerprint_whitespace(b) for b in chunk)
    mask = 0xFFFFFFFF
    multiplier = 1540483477
    h = 1 ^ normalized_length
    word = 0
    shift = 0
    with path.open('rb') as source:
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            for byte in chunk:
                if _is_fingerprint_whitespace(byte):
                    continue
                word |= byte << shift
                shift += 8
                if shift == 32:
                    mixed = (word * multiplier) & mask
                    mixed = (((mixed ^ (mixed >> 24)) * multiplier) & mask)
                    h = ((h * multiplier) ^ mixed) & mask
                    word = 0
                    shift = 0
    if shift:
        h = ((h ^ word) * multiplier) & mask
    mixed = ((h ^ (h >> 13)) * multiplier) & mask
    return (mixed ^ (mixed >> 15)) & mask


def identify_fingerprints(paths_by_fingerprint):
    """Return exact CurseForge matches keyed by input fingerprint. No fuzzy hits."""
    if not paths_by_fingerprint:
        return {}
    if not has_api_key():
        return {}
    from ..download_http import make_request, open_url, http_error_message
    matches = {}
    fingerprints = list(paths_by_fingerprint)
    for offset in range(0, len(fingerprints), 100):
        batch = fingerprints[offset:offset + 100]
        body = json.dumps({'fingerprints': batch}).encode('utf-8')
        request = make_request(_API + '/fingerprints/432', data=body, headers={
            'Accept': 'application/json', 'Content-Type': 'application/json',
            'User-Agent': mr._UA,
        })
        try:
            with open_url(request, timeout=20) as response:
                raw = response.read(mr._MAX_JSON_BYTES + 1)
            if len(raw) > mr._MAX_JSON_BYTES:
                raise mr.ModrinthError('CurseForge fingerprint response exceeded the size limit.')
            payload = json.loads(raw.decode('utf-8'))['data']
        except urllib.error.HTTPError as exc:
            raise mr.ModrinthError(http_error_message(exc.code, curseforge=True)) from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise mr.ModrinthError('Could not identify files through CurseForge. Modrinth matches may still be available.') from exc
        requested = set(batch)
        for item in payload.get('exactMatches', []):
            try:
                fingerprint = int(item['id'])
                file_data = item['file']
                file_fingerprint = int(file_data.get('fileFingerprint', -1))
                project_id = int(file_data['modId'])
                file_id = int(file_data['id'])
            except (KeyError, TypeError, ValueError):
                continue
            # Require both the exact-match bucket and the matched file's own
            # fingerprint to agree with the submitted local fingerprint.
            if fingerprint not in requested or file_fingerprint != fingerprint:
                continue
            matches[fingerprint] = {
                'project_id': f'cf:{project_id}', 'version_id': str(file_id),
                'version': file_data.get('displayName', ''),
                'filename': file_data.get('fileName', ''),
                'fingerprint': fingerprint, 'identity': 'exact-curseforge-fingerprint',
                'sha1': next((h.get('value', '') for h in file_data.get('hashes', [])
                              if h.get('algo') == 1), ''),
            }
    return matches


def _get(route):
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
    if not mc_version or loader not in _LOADERS:
        raise mr.ModrinthError('Set the Minecraft version and a supported mod loader in Setup first.')
    pid = project_id.removeprefix('cf:')
    if not pid.isdigit(): raise mr.ModrinthError('Invalid CurseForge project ID.')
    files=[]
    for offset in range(0, 1000, 50):
        args=urllib.parse.urlencode({'gameVersion':mc_version,'modLoaderType':_LOADERS[loader],'pageSize':50,'index':offset})
        page=_get(f'/mods/{pid}/files?{args}')
        # The API filters by loader, while this local guard verifies the
        # requested Minecraft version before a file can be offered. Missing
        # compatibility metadata is not proof of compatibility.
        files.extend(
            _file(pid, v) for v in page
            if v.get('isAvailable', True)
            and mc_version in v.get('gameVersions', [])
        )
        if len(page)<50: break
    return files

def project_url(project_id):
    """Resolve an identified CurseForge project ID to its official page URL."""
    pid = str(project_id).removeprefix('cf:')
    if not pid.isdigit():
        raise mr.ModrinthError('Invalid CurseForge project ID; cannot open its source page.')
    details = _get(f'/mods/{pid}')
    slug = details.get('slug', '') if isinstance(details, dict) else ''
    if not isinstance(slug, str) or not slug or any(ord(ch) < 32 for ch in slug):
        raise mr.ModrinthError('CurseForge did not provide a usable project slug. The exact file identity remains recorded.')
    return 'https://www.curseforge.com/minecraft/mc-mods/' + urllib.parse.quote(slug, safe='')


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
