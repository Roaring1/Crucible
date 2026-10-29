"""Exact mod identity, compatible release selection and recoverable replacements."""
from __future__ import annotations
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from . import modrinth as mr


class IdentityMatches(dict):
    """Exact identity matches, with an optional provider-degradation warning."""

    warning = None


def identify_files(server_path, paths):
    """Identify installed JARs independently through exact provider hashes.

    Modrinth uses SHA-1 and CurseForge uses its normalized fingerprint. A
    failure in either provider must not prevent the other from being queried;
    exact matches from a working provider remain useful when the other is down.
    JAR contents are never uploaded and duplicate paths are preserved.
    """
    from . import curseforge as cf

    files = [Path(p) for p in paths if Path(p).is_file() and not Path(p).is_symlink()]
    if not files:
        return {}

    record_path = Path(server_path) / '.crucible' / 'added-mods.json'
    record = mr._load_record(record_path)
    by_sha1 = {}
    for path in files:
        by_sha1.setdefault(mr._sha1(path), []).append(path)

    found = IdentityMatches()
    provider_errors = []

    # Provider 1: Modrinth exact SHA-1. Keep this whole provider operation
    # independent from CurseForge, including network and malformed-response
    # errors, so failure here never short-circuits the second provider.
    try:
        items = list(by_sha1)
        for offset in range(0, len(items), 100):
            batch = items[offset:offset + 100]
            req = mr.urllib.request.Request(
                mr._API + '/version_files',
                data=json.dumps({'hashes': batch, 'algorithm': 'sha1'}).encode(),
                headers={'User-Agent': mr._UA, 'Content-Type': 'application/json'},
            )
            try:
                with mr.urllib.request.urlopen(req, timeout=20) as response:
                    raw = response.read(mr._MAX_JSON_BYTES + 1)
                if len(raw) > mr._MAX_JSON_BYTES:
                    raise mr.ModrinthError('Identity response was too large.')
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise mr.ModrinthError('Identity response had an unexpected format.')
            except (OSError, ValueError) as exc:
                raise mr.ModrinthError(
                    'Could not identify mods on Modrinth: ' + (str(exc) or 'provider request failed')
                    + '. Existing files have not been changed.'
                ) from exc

            for digest, version in result.items():
                if digest not in by_sha1:
                    continue
                if not isinstance(version, dict):
                    raise mr.ModrinthError('Modrinth identity response contained a malformed version record.')
                version_files = version.get('files', [])
                if not isinstance(version_files, list) or any(not isinstance(f, dict) for f in version_files):
                    raise mr.ModrinthError('Modrinth identity response contained malformed file metadata.')
                if any(not isinstance(f.get('hashes'), dict) for f in version_files):
                    raise mr.ModrinthError('Modrinth identity response contained malformed file hashes.')
                exact_file_present = any(
                    f['hashes'].get('sha1') == digest for f in version_files
                )
                if not exact_file_present:
                    continue
                project_id, version_id = version.get('project_id'), version.get('id')
                if (not isinstance(project_id, str) or not project_id or project_id.startswith('cf:')
                        or not isinstance(version_id, str) or not version_id):
                    raise mr.ModrinthError('Modrinth identity response contained an invalid provider identity.')
                meta = {
                    'project_id': project_id, 'version_id': version_id,
                    'version': version.get('version_number', ''), 'sha1': digest,
                    'identity': 'exact-sha1',
                }
                for path in by_sha1[digest]:
                    found[path.name] = dict(meta)
    except (mr.ModrinthError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        provider_errors.append(('Modrinth', 'could not be checked: ' + (str(exc) or 'provider lookup failed')))

    # Provider 2: CurseForge exact fingerprint. A missing key means this
    # provider is intentionally skipped, not an error; Modrinth remains usable
    # without CurseForge credentials. Unsafe or invalid configured key files
    # raise a user-facing error which is handled like any other CF failure.
    try:
        if cf.has_api_key():
            by_fingerprint = {}
            for path in files:
                fingerprint = cf.fingerprint_file(path)
                by_fingerprint.setdefault(fingerprint, []).append(path)
            cf_matches = cf.identify_fingerprints(by_fingerprint)
            for fingerprint in getattr(cf_matches, 'ambiguous_fingerprints', ()):
                provider_errors.append((
                    'CurseForge',
                    f'fingerprint {fingerprint} matched multiple exact files; identity was left unlinked',
                ))
            for fingerprint, meta in cf_matches.items():
                for path in by_fingerprint.get(fingerprint, []):
                    # Prefer a prior provider identity only when this exact file
                    # is still identified as that same project on this provider.
                    prior = found.get(path.name, {})
                    existing = record.get(path.name, {})
                    existing_project = existing.get('project_id', '')
                    if prior and existing_project == meta['project_id']:
                        found[path.name] = dict(meta)
                    elif not prior:
                        found[path.name] = dict(meta)
    except (mr.ModrinthError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        provider_errors.append(('CurseForge', 'could not be checked: ' + (str(exc) or 'provider lookup failed')))

    # Never turn an error from one provider into a false "no match" result.
    # Conversely, do not throw away successful exact matches from the other.
    if not found and provider_errors:
        details = '; '.join(f'{provider} {message}' for provider, message in provider_errors)
        raise mr.ModrinthError('Could not complete exact mod identification. ' + details)
    if found and provider_errors:
        found.warning = 'Partial online identity results. ' + '; '.join(
            f'{provider} {message}' for provider, message in provider_errors
        )

    for filename, meta in found.items():
        prior = record.get(filename, {})
        merged = {**prior, **meta}
        if prior.get('identity') != meta.get('identity') and meta.get('identity') == 'exact-sha1':
            # Provider-specific data must not leak across a provider identity
            # change: a Modrinth SHA-1 record is not a CurseForge fingerprint.
            merged.pop('fingerprint', None)
            merged.pop('filename', None)
        record[filename] = merged
    if found:
        _write_record(record_path, record)
    return found


def verified_identity_for_file(server_path, filename, file_path):
    """Return a saved identity only while its exact local hash still matches.

    Older records can outlive manual replacement of a JAR. Never use those
    records for version changes, dependency checks, or source links unless the
    current file still matches its recorded SHA-1 or CurseForge fingerprint.
    """
    path = Path(file_path)
    if Path(filename).name != filename or "\\" in filename:
        return None
    mods_dir = Path(server_path) / 'mods'
    state_dir = Path(server_path) / '.crucible'
    # Do not verify or consume identity through a symlinked server mods/state
    # directory, or for a path other than this instance's actual mods entry.
    if mods_dir.is_symlink() or state_dir.is_symlink():
        return None
    expected_path = mods_dir / filename
    if path.absolute() != expected_path.absolute():
        return None
    if not path.is_file() or path.is_symlink():
        return None

    record_path = state_dir / 'added-mods.json'
    if record_path.is_symlink():
        return None
    record = mr._load_record(record_path)
    base = filename.removesuffix('.disabled')
    meta = record.get(filename) or record.get(base)
    if not isinstance(meta, dict) or not meta.get('project_id') or not meta.get('version_id'):
        return None

    recorded_sha1 = meta.get('sha1', '')
    valid_sha1 = (
        isinstance(recorded_sha1, str) and len(recorded_sha1) == 40
        and all(ch in '0123456789abcdefABCDEF' for ch in recorded_sha1)
    )
    identity = meta.get('identity')
    project_id = meta['project_id']

    # A Modrinth exact-SHA1 identity cannot be paired with a CurseForge ID or
    # retain a fingerprint from an older provider identity.
    if identity == 'exact-sha1':
        if project_id.startswith('cf:') or not valid_sha1:
            return None
        try:
            if mr._sha1(path).lower() != recorded_sha1.lower():
                return None
        except OSError:
            return None
        return {key: value for key, value in meta.items() if key not in ('fingerprint', 'filename')}

    # Exact CurseForge records require both their namespace and fingerprint.
    # When SHA-1 was also supplied, require it to agree too.
    if identity == 'exact-curseforge-fingerprint':
        recorded_fingerprint = meta.get('fingerprint')
        if not project_id.startswith('cf:') or type(recorded_fingerprint) is not int:
            return None
        if recorded_sha1 and not valid_sha1:
            return None
        from . import curseforge as cf
        try:
            if cf.fingerprint_file(path) != recorded_fingerprint:
                return None
            if valid_sha1 and mr._sha1(path).lower() != recorded_sha1.lower():
                return None
        except OSError:
            return None
        return meta

    # Install/update records pre-date the explicit identity discriminator but
    # are written only after a selected release's SHA-1 was verified on disk.
    # Trust only that local checksum; unknown identity labels fail closed.
    if identity not in (None, '') or not valid_sha1:
        return None
    try:
        return meta if mr._sha1(path).lower() == recorded_sha1.lower() else None
    except OSError:
        return None



def _write_record(path, record):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd, tmp=tempfile.mkstemp(prefix='.record-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as out:
            json.dump(record,out,indent=2); out.flush(); os.fsync(out.fileno())
        os.replace(tmp,path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _mod_file(v):
    project_id = v.get('project_id') if isinstance(v, dict) else None
    version_id = v.get('id') if isinstance(v, dict) else None
    if (not isinstance(project_id, str) or not project_id or project_id.startswith('cf:')
            or not isinstance(version_id, str) or not version_id):
        raise mr.ModrinthError('Modrinth returned a release with an invalid provider identity.')
    files = v.get('files', [])
    if not isinstance(files, list) or any(not isinstance(item, dict) for item in files):
        raise mr.ModrinthError('Modrinth returned malformed file metadata.')
    pf = mr._pick_primary(files)
    if not pf:
        raise mr.ModrinthError('This version has no downloadable file.')
    if not isinstance(pf, dict):
        raise mr.ModrinthError('Modrinth returned malformed primary-file metadata.')
    filename = pf.get('filename')
    if not isinstance(filename, str) or Path(filename).name != filename or '\\' in filename or not filename.endswith('.jar'):
        raise mr.ModrinthError('Modrinth returned an unsafe or non-JAR filename.')
    hashes = pf.get('hashes', {}) or {}
    if not isinstance(hashes, dict):
        raise mr.ModrinthError('Modrinth returned malformed file hashes.')
    sha1 = hashes.get('sha1', '')
    if (not isinstance(sha1, str) or len(sha1) != 40
            or any(ch not in '0123456789abcdefABCDEF' for ch in sha1)):
        raise mr.ModrinthError('Modrinth did not provide a valid SHA-1 for the selected release; no files were changed.')
    try:
        size = int(pf.get('size', 0) or 0)
    except (TypeError, ValueError) as exc:
        raise mr.ModrinthError('Modrinth returned an invalid file size.') from exc
    if size < 0:
        raise mr.ModrinthError('Modrinth returned an invalid file size.')
    dependencies = v.get('dependencies', [])
    if not isinstance(dependencies, list) or any(not isinstance(dep, dict) for dep in dependencies):
        raise mr.ModrinthError('Modrinth returned malformed dependency metadata.')
    url = pf.get('url', '') or ''
    if not isinstance(url, str):
        raise mr.ModrinthError('Modrinth returned an invalid download URL.')
    return mr.ModFile(
        project_id=project_id, title=v.get('name', ''), version_id=version_id,
        version_number=v.get('version_number', ''), filename=filename, url=url,
        sha1=sha1, size=size, dependencies=dependencies,
    )


def list_versions(project_id, *, loader, mc_version):
    if project_id.startswith('cf:'):
        from .curseforge import list_versions as cf_versions
        return cf_versions(project_id,loader=loader,mc_version=mc_version)
    if not loader or not mc_version or loader=='vanilla':
        raise mr.ModrinthError('Set this server’s Minecraft version and mod loader in Setup before choosing a mod version.')
    args=mr.urllib.parse.urlencode({'loaders':json.dumps([loader]),'game_versions':json.dumps([mc_version])})
    versions=mr._get(f'{mr._API}/project/{mr.urllib.parse.quote(project_id,safe="")}/version?{args}')
    if not isinstance(versions, list):
        raise mr.ModrinthError('Modrinth returned an unexpected release-list format.')
    # The API endpoint should only return releases for the selected project.
    # Validate that contract rather than trusting an unrelated version ID.
    out=[]
    for version in versions:
        if not isinstance(version, dict):
            raise mr.ModrinthError('Modrinth returned malformed release metadata.')
        if version.get('project_id') != project_id or not version.get('id'):
            raise mr.ModrinthError('Modrinth returned a release for a different or unknown project.')
        release_files = version.get('files', [])
        loaders = version.get('loaders', [])
        game_versions = version.get('game_versions', [])
        if (not isinstance(release_files, list) or any(not isinstance(item, dict) for item in release_files)
                or not isinstance(loaders, list) or not isinstance(game_versions, list)):
            raise mr.ModrinthError('Modrinth returned malformed file or compatibility metadata for a release.')
        if loader in loaders and mc_version in game_versions and release_files:
            release = _mod_file(version)
            if release.sha1:
                out.append(release)
    # Include older/beta versions, not just upgrades.
    return out


def resolve_selected(main, *, loader, mc_version, _seen=None):
    seen={} if _seen is None else _seen
    if main.project_id in seen:
        if seen[main.project_id] != main.version_id:
            raise mr.ModrinthError("Required dependencies demand conflicting versions of " + main.project_id + ". No files were changed.")
        return []
    seen[main.project_id] = main.version_id
    out=[main]
    if main.project_id.startswith('cf:'):
        if not isinstance(main.dependencies, list) or any(
            not isinstance(project_id, str) or not project_id.startswith('cf:')
            for project_id in main.dependencies
        ):
            raise mr.ModrinthError('CurseForge returned a required dependency without a namespaced CurseForge project ID. No files were changed.')
        deps=[{'project_id':p,'dependency_type':'required'} for p in main.dependencies]
    else: deps=main.dependencies
    for dep in deps:
        if dep.get('dependency_type')!='required': continue
        pid=dep.get('project_id')
        vid=dep.get('version_id')
        if pid and pid.startswith('cf:'):
            # Provider identity takes precedence over a version pin: never send
            # a CurseForge project/file selection to Modrinth's /version API.
            versions=list_versions(pid,loader=loader,mc_version=mc_version)
            selected=next((release for release in versions if vid and release.version_id == str(vid)), None) if vid else (versions[0] if versions else None)
            if selected is None:
                raise mr.ModrinthError(f'Required CurseForge dependency {pid} has no matching compatible release. No files were changed.')
        elif vid:
            raw=mr._get(f'{mr._API}/version/{mr.urllib.parse.quote(vid,safe="")}')
            if loader not in raw.get('loaders',[]) or mc_version not in raw.get('game_versions',[]):
                raise mr.ModrinthError('A pinned dependency is incompatible with this server. No files were changed.')
            if pid and raw.get('project_id') != pid:
                raise mr.ModrinthError('A pinned dependency version did not belong to the selected project. No files were changed.')
            selected=_mod_file(raw)
        elif pid:
            versions=list_versions(pid,loader=loader,mc_version=mc_version)
            if not versions: raise mr.ModrinthError(f'Required dependency {pid} has no compatible release.')
            selected=versions[0]
        else: raise mr.ModrinthError('A required dependency has no resolvable project or version.')
        out.extend(resolve_selected(selected,loader=loader,mc_version=mc_version,_seen=seen))
    return out


def replace_files(server_path, old_filename, files, *, before_commit=None):
    """Stage and verify everything first; retain originals and a recovery receipt.

    Roll back published files and the identity record on ordinary I/O failures.
    An interrupted operation leaves its history directory for manual recovery.
    """
    server=Path(server_path)
    mods=server/'mods'
    if not files: raise mr.ModrinthError('No replacement files were selected.')
    if old_filename is not None and (Path(old_filename).name != old_filename or '\\' in old_filename):
        raise mr.ModrinthError('Unsafe original filename.')
    if mods.is_symlink() or (server/'.crucible').is_symlink():
        raise mr.ModrinthError('Refusing a symlinked mod or state directory.')
    for mf in files:
        if Path(mf.filename).name!=mf.filename or '\\' in mf.filename or not mf.filename.endswith('.jar'):
            raise mr.ModrinthError('Unsafe replacement filename.')
        if not mf.sha1 or len(mf.sha1)!=40:
            raise mr.ModrinthError('No verifiable SHA-1 was supplied; original kept.')
        if not mf.url: raise mr.ModrinthError('Direct download is restricted; download from the project page and use Add .jar.')
    names=[f.filename for f in files]
    if len(set(names))!=len(names): raise mr.ModrinthError('Two projects would overwrite the same filename.')
    original=mods/old_filename if old_filename is not None else None
    if original is not None and (not original.is_file() or original.is_symlink()):
        raise mr.ModrinthError("The original mod is missing or symlinked; refresh the mod list.")
    original_hash=mr._sha1(original) if original is not None else None
    mods.mkdir(parents=True, exist_ok=True)
    state=server/'.crucible'; state.mkdir(parents=True,exist_ok=True)
    lock=state/'mod-change.lock'
    try: fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    except FileExistsError as exc: raise mr.ModrinthError('Another mod change is active, or an interrupted change needs recovery. Review .crucible/mod-history before clearing mod-change.lock.') from exc
    os.close(fd)
    try:
        with tempfile.TemporaryDirectory(prefix='mod-stage-',dir=state) as tmp:
            result=mr.install_files(tmp,files)
            if result.failed: return result
            if original is not None and (not original.is_file() or mr._sha1(original) != original_hash):
                raise mr.ModrinthError("The original mod changed while downloading. No replacement was applied.")
            if before_commit is not None:
                before_commit()
            record_path=state/'added-mods.json'
            record=mr._load_record(record_path)
            projects={f.project_id for f in files}
            affected=set(names)
            if old_filename is not None: affected.add(old_filename)
            affected.update(n for n,m in record.items() if m.get('project_id') in projects and Path(n).name==n and '\\' not in n)
            disabled=bool(old_filename and old_filename.endswith('.disabled'))
            dest_names={f.filename:(f.filename+'.disabled' if disabled and i==0 else f.filename) for i,f in enumerate(files)}
            affected.update(dest_names.values())
            history=state/'mod-history'/uuid.uuid4().hex
            history.mkdir(parents=True)
            saved=[]; published=[]
            for name in affected:
                p=mods/name
                if p.is_symlink(): raise mr.ModrinthError('Refusing to replace a symlinked mod.')
                if p.exists(): shutil.copy2(p,history/name); saved.append(name)
            old_record=record_path.read_bytes() if record_path.exists() else None
            if old_record is not None: (history/'added-mods.json').write_bytes(old_record)
            receipt={'original':old_filename,'backed_up':saved,'installed':list(dest_names.values()),'status':'prepared'}
            (history/'receipt.json').write_text(json.dumps(receipt,indent=2))
            try:
                for name in saved: (mods/name).unlink()
                for mf in files:
                    dest=mods/dest_names[mf.filename]
                    os.replace(Path(tmp)/'mods'/mf.filename,dest); published.append(dest)
                for name in affected: record.pop(name,None)
                staged=mr.added_mods(tmp)
                for mf in files: record[dest_names[mf.filename]]=staged[mf.filename]
                _write_record(record_path,record)
                receipt['status']='complete'
                (history/'receipt.json').write_text(json.dumps(receipt,indent=2))
            except Exception:
                for dest in published: dest.unlink(missing_ok=True)
                for name in saved: shutil.copy2(history/name,mods/name)
                if old_record is None: record_path.unlink(missing_ok=True)
                else: record_path.write_bytes(old_record)
                receipt['status']='rolled-back'; (history/'receipt.json').write_text(json.dumps(receipt,indent=2))
                raise
            return result
    finally:
        lock.unlink(missing_ok=True)
