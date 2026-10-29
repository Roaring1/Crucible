"""Exact mod identity, compatible release selection and recoverable replacements."""
from __future__ import annotations
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from . import modrinth as mr


def identify_files(server_path, paths):
    """Identify installed JARs by exact Modrinth SHA-1 and CurseForge fingerprint.

    JAR contents are never uploaded. Duplicate bytes under different filenames
    are preserved, and CurseForge is optional: Modrinth continues to work when
    no CurseForge key is configured. Approximate/fuzzy matches are never used.
    """
    from . import curseforge as cf

    files = [Path(p) for p in paths if Path(p).is_file() and not Path(p).is_symlink()]
    if not files:
        return {}
    by_sha1 = {}
    for path in files:
        by_sha1.setdefault(mr._sha1(path), []).append(path)
    found = {}
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
        except (OSError, ValueError) as exc:
            raise mr.ModrinthError('Could not identify mods on Modrinth. Existing files have not been changed.') from exc
        for digest, version in result.items():
            if digest not in by_sha1:
                continue
            if not any(f.get('hashes', {}).get('sha1') == digest for f in version.get('files', [])):
                continue
            meta = {
                'project_id': version['project_id'], 'version_id': version['id'],
                'version': version.get('version_number', ''), 'sha1': digest,
                'identity': 'exact-sha1',
            }
            for path in by_sha1[digest]:
                found[path.name] = dict(meta)

    record_path = Path(server_path) / '.crucible' / 'added-mods.json'
    record = mr._load_record(record_path)

    # CurseForge fingerprints are a different algorithm from SHA-1. Compute
    # and query them only when the user has configured a private API key.
    if cf.has_api_key():
        by_fingerprint = {}
        for path in files:
            fingerprint = cf.fingerprint_file(path)
            by_fingerprint.setdefault(fingerprint, []).append(path)
        cf_matches = cf.identify_fingerprints(by_fingerprint)
        for fingerprint, meta in cf_matches.items():
            for path in by_fingerprint.get(fingerprint, []):
                # Prefer an already-found Modrinth exact match deterministically
                # if the same artifact is published on both providers. Existing
                # provider identity is retained when it remains an exact match.
                prior = found.get(path.name, {})
                existing = record.get(path.name, {})
                existing_project = existing.get('project_id', '')
                if prior and existing_project == meta['project_id']:
                    found[path.name] = dict(meta)
                elif not prior:
                    found[path.name] = dict(meta)

    for filename, meta in found.items():
        record[filename] = {**record.get(filename, {}), **meta}
    if found:
        _write_record(record_path, record)
    return found


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
    pf=mr._pick_primary(v.get('files',[]))
    if not pf: raise mr.ModrinthError('This version has no downloadable file.')
    return mr.ModFile(project_id=v['project_id'],title=v.get('name',''),version_id=v['id'],version_number=v.get('version_number',''),filename=pf['filename'],url=pf.get('url',''),sha1=pf.get('hashes',{}).get('sha1',''),size=pf.get('size',0),dependencies=v.get('dependencies',[]))


def list_versions(project_id, *, loader, mc_version):
    if project_id.startswith('cf:'):
        from .curseforge import list_versions as cf_versions
        return cf_versions(project_id,loader=loader,mc_version=mc_version)
    if not loader or not mc_version or loader=='vanilla':
        raise mr.ModrinthError('Set this server’s Minecraft version and mod loader in Setup before choosing a mod version.')
    args=mr.urllib.parse.urlencode({'loaders':json.dumps([loader]),'game_versions':json.dumps([mc_version])})
    versions=mr._get(f'{mr._API}/project/{mr.urllib.parse.quote(project_id,safe="")}/version?{args}')
    # API filtering plus local verification. Include older/beta versions, not just upgrades.
    return [_mod_file(v) for v in versions if loader in v.get('loaders',[]) and mc_version in v.get('game_versions',[]) and v.get('files')]


def resolve_selected(main, *, loader, mc_version, _seen=None):
    seen={} if _seen is None else _seen
    if main.project_id in seen:
        if seen[main.project_id] != main.version_id:
            raise mr.ModrinthError("Required dependencies demand conflicting versions of " + main.project_id + ". No files were changed.")
        return []
    seen[main.project_id] = main.version_id
    out=[main]
    if main.project_id.startswith('cf:'):
        deps=[{'project_id':p,'dependency_type':'required'} for p in main.dependencies]
    else: deps=main.dependencies
    for dep in deps:
        if dep.get('dependency_type')!='required': continue
        pid=dep.get('project_id')
        vid=dep.get('version_id')
        if vid:
            raw=mr._get(f'{mr._API}/version/{mr.urllib.parse.quote(vid,safe="")}')
            if loader not in raw.get('loaders',[]) or mc_version not in raw.get('game_versions',[]):
                raise mr.ModrinthError('A pinned dependency is incompatible with this server. No files were changed.')
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
