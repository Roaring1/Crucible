"""Behavioral regression tests for the server-manager refinement, no live server required."""
import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from crucible.mods import modrinth as mr
from crucible.mods import versions as v
from crucible.mods import curseforge as cf
from crucible.mods.mod_manager import ModManager, ModEntry
from crucible.exporters import client_export
from crucible.process.resource_monitor import machine_cpu_percent
from crucible.diagnostics.loadcheck import parse_crash_log, latest_crash_text


def mf(name='new.jar', data=b'new', project='project'):
    return mr.ModFile(project,'Demo','v2','2.0',name,'https://example.invalid/file',hashlib.sha1(data).hexdigest(),len(data))


def server(tmp_path):
    (tmp_path/'mods').mkdir()
    (tmp_path/'mods/old.jar').write_bytes(b'old')
    return tmp_path


def downloader(monkeypatch, data=b'new'):
    monkeypatch.setattr(mr,'_download',lambda url,dest: (dest.write_bytes(data) and len(data)))


@pytest.mark.parametrize('raw,cpus,expected',[(42,8,5.25),(100,16,6.25),(1600,16,100),(0,8,0),(-5,8,0),(100,0,100)])
def test_cpu_machine_denominator(raw,cpus,expected):
    assert machine_cpu_percent(raw,cpus)==expected


def test_failed_download_keeps_original(tmp_path,monkeypatch):
    server(tmp_path)
    def fail(*a): raise mr.ModrinthError('offline')
    monkeypatch.setattr(mr,'_download',fail)
    result=mr.apply_update(tmp_path,'old.jar',mf())
    assert result.failed
    assert (tmp_path/'mods/old.jar').read_bytes()==b'old'
    assert not (tmp_path/'mods/new.jar').exists()


def test_bad_hash_keeps_original(tmp_path,monkeypatch):
    server(tmp_path); downloader(monkeypatch,b'corrupted')
    result=v.replace_files(tmp_path,'old.jar',[mf()])
    assert result.failed and (tmp_path/'mods/old.jar').read_bytes()==b'old'


def test_success_retains_recoverable_original(tmp_path,monkeypatch):
    server(tmp_path);downloader(monkeypatch)
    assert not v.replace_files(tmp_path,'old.jar',[mf()]).failed
    assert not (tmp_path/'mods/old.jar').exists()
    assert (tmp_path/'mods/new.jar').read_bytes()==b'new'
    history=list((tmp_path/'.crucible/mod-history').iterdir())
    assert (history[0]/'old.jar').read_bytes()==b'old'
    assert json.loads((history[0]/'receipt.json').read_text())['status']=='complete'


def test_same_filename_really_replaced(tmp_path,monkeypatch):
    server(tmp_path);downloader(monkeypatch)
    v.replace_files(tmp_path,'old.jar',[mf('old.jar')])
    assert (tmp_path/'mods/old.jar').read_bytes()==b'new'


def test_disabled_state_survives_version_change(tmp_path,monkeypatch):
    server(tmp_path);(tmp_path/'mods/old.jar').rename(tmp_path/'mods/old.jar.disabled');downloader(monkeypatch)
    v.replace_files(tmp_path,'old.jar.disabled',[mf()])
    assert (tmp_path/'mods/new.jar.disabled').exists()
    assert not (tmp_path/'mods/new.jar').exists()


def test_publish_failure_rolls_back_everything(tmp_path,monkeypatch):
    server(tmp_path);downloader(monkeypatch)
    real_replace=v.os.replace
    def fail(src,dst):
        if Path(dst)==tmp_path/'mods/new.jar': raise OSError('disk failure')
        return real_replace(src,dst)
    monkeypatch.setattr(v.os,'replace',fail)
    with pytest.raises(OSError): v.replace_files(tmp_path,'old.jar',[mf()])
    assert (tmp_path/'mods/old.jar').read_bytes()==b'old'
    assert not (tmp_path/'mods/new.jar').exists()
    assert not (tmp_path/'.crucible/mod-change.lock').exists()


@pytest.mark.parametrize('name',['../escape.jar','/tmp/escape.jar','bad\\escape.jar','not-a-jar.zip'])
def test_unsafe_provider_filename_rejected(tmp_path,monkeypatch,name):
    server(tmp_path);downloader(monkeypatch)
    with pytest.raises(mr.ModrinthError): v.replace_files(tmp_path,'old.jar',[mf(name)])
    assert (tmp_path/'mods/old.jar').exists()
    assert mr.install_files(tmp_path,[mf(name)]).failed


def test_incomplete_dependency_download_never_retires_original(tmp_path,monkeypatch):
    server(tmp_path)
    def fetch(url,dest):
        if dest.name=='dep.jar':raise mr.ModrinthError('dependency unavailable')
        return dest.write_bytes(b'new')
    monkeypatch.setattr(mr,'_download',fetch)
    result=v.replace_files(tmp_path,'old.jar',[mf(),mf('dep.jar',project='dependency')])
    assert result.failed and (tmp_path/'mods/old.jar').exists()
    assert not (tmp_path/'mods/new.jar').exists()


def test_unverified_download_is_refused(tmp_path):
    server(tmp_path);file=mf();file.sha1=''
    with pytest.raises(mr.ModrinthError):v.replace_files(tmp_path,'old.jar',[file])


def test_restricted_curseforge_download_is_not_fabricated(tmp_path):
    server(tmp_path);file=mf();file.url=''
    with pytest.raises(mr.ModrinthError,match='restricted'):v.replace_files(tmp_path,'old.jar',[file])


def test_concurrent_transaction_refused(tmp_path):
    server(tmp_path);(tmp_path/'.crucible').mkdir();(tmp_path/'.crucible/mod-change.lock').touch()
    with pytest.raises(mr.ModrinthError,match='Another'):v.replace_files(tmp_path,'old.jar',[mf()])


def test_symlinked_mods_refused(tmp_path):
    (tmp_path/'outside').mkdir();(tmp_path/'mods').symlink_to(tmp_path/'outside',target_is_directory=True)
    with pytest.raises(mr.ModrinthError,match='symlink'):v.replace_files(tmp_path,'old.jar',[mf()])


def test_curseforge_requires_explicit_key(monkeypatch,tmp_path):
    monkeypatch.delenv('CURSEFORGE_API_KEY',raising=False);monkeypatch.delenv('CURSEFORGE_API_KEY_FILE',raising=False);monkeypatch.setattr(cf,'_session_key','');monkeypatch.setattr(cf,'_DEFAULT_KEY_FILE',tmp_path/'missing')
    with pytest.raises(mr.ModrinthError,match='API key'):cf.search('sodium')


def test_curseforge_search_preserves_provider_identity(monkeypatch):
    routes=[]
    monkeypatch.setattr(cf,'_get',lambda route: routes.append(route) or [{'id':123,'name':'Example','slug':'example','downloadCount':99}])
    result=cf.search('example',loader='neoforge',mc_version='1.21.1')
    assert result[0].project_id=='cf:123'
    assert result[0].page_url()=='https://www.curseforge.com/minecraft/mc-mods/example'
    assert 'modLoaderType=6' in routes[0] and 'gameVersion=1.21.1' in routes[0]


def test_curseforge_version_keeps_missing_download_url(monkeypatch):
    monkeypatch.setattr(cf,'_get',lambda _: [{'id':7,'fileName':'a.jar','downloadUrl':None,'hashes':[{'algo':1,'value':'a'*40}]}])
    assert cf.list_versions('cf:123',loader='forge',mc_version='1.20.1')[0].url==''


def test_versions_include_downgrades_and_filter_locally(monkeypatch):
    def release(id,loader='fabric',mc='1.21.1'):
        return {'id':id,'project_id':'p','version_number':id,'loaders':[loader],'game_versions':[mc],'files':[{'filename':id+'.jar','url':'https://example.invalid/a','hashes':{'sha1':'a'*40}}]}
    monkeypatch.setattr(mr,'_get',lambda _: [release('new'),release('old'),release('wrong','forge'),release('wrongmc',mc='1.20.1')])
    assert [f.version_id for f in v.list_versions('p',loader='fabric',mc_version='1.21.1')]==['new','old']


def test_pinned_dependency_must_match_platform(monkeypatch):
    main=mf();main.dependencies=[{'dependency_type':'required','version_id':'fixed'}]
    monkeypatch.setattr(mr,'_get',lambda _: {'loaders':['forge'],'game_versions':['1.7.10']})
    with pytest.raises(mr.ModrinthError,match='incompatible'):v.resolve_selected(main,loader='fabric',mc_version='1.21.1')


def jar(path,side='*',name='demo'):
    path.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(path,'w') as z:z.writestr('fabric.mod.json',json.dumps({'id':name,'name':name,'version':'1.0','environment':side}))


@pytest.mark.parametrize('declared,expected',[('client','client'),('server','server'),('*','both'),('unexpected','unknown')])
def test_explicit_side_metadata(tmp_path,declared,expected):
    path=tmp_path/'demo.jar';jar(path,declared)
    entry=ModEntry(path.name,path,True,path.stat().st_size)
    ModManager(SimpleNamespace(path=str(tmp_path))).inspect_jar(entry)
    assert entry.side==expected


def test_forge_dependency_side_does_not_classify_whole_mod(tmp_path):
    path=tmp_path/'forge.jar'
    with zipfile.ZipFile(path,'w') as z:z.writestr('META-INF/mods.toml','modLoader="javafml"\n[[mods]]\nmodId="common"\nversion="1"\n[[dependencies.common]]\nmodId="forge"\nside="CLIENT"\n')
    entry=ModEntry(path.name,path,True,path.stat().st_size);ModManager(SimpleNamespace(path=str(tmp_path))).inspect_jar(entry)
    assert entry.side=='unknown' and entry.mod_id=='common'


def test_client_export_has_correct_environment_and_no_world(tmp_path):
    server=tmp_path/'server';jar(server/'mods/shared.jar','*');jar(server/'mods/server.jar','server');jar(server/'mods/client.jar','client')
    (server/'world').mkdir();(server/'world/secret.dat').write_text('not for client')
    inst=SimpleNamespace(path=str(server),name='Example',minecraft_version='1.21.1',loader='fabric',loader_version='0.16.10')
    for fmt,suffix in [('prism','zip'),('mrpack','mrpack'),('curseforge','zip')]:
        out=tmp_path/f'client-{fmt}.{suffix}';result=client_export.export(inst,out,fmt)
        assert result.ok and result.mod_count==2
        with zipfile.ZipFile(out) as z:
            assert z.testzip() is None
            assert not any('world/' in n or n.endswith('server.jar') for n in z.namelist())


def test_client_source_adds_only_declared_client_extras(tmp_path):
    server=tmp_path/'server';jar(server/'mods/shared.jar')
    source=tmp_path/'prism';jar(source/'.minecraft/mods/client.jar','client');jar(source/'.minecraft/mods/old-shared.jar','*')
    (source/'mmc-pack.json').write_text(json.dumps({'components':[{'uid':'net.minecraft','version':'1.21.1'},{'uid':'net.fabricmc.fabric-loader','version':'0.16.10'}]}))
    inst=SimpleNamespace(path=str(server),name='Example',minecraft_version='1.21.1',loader='fabric',loader_version='0.16.10')
    result=client_export.export(inst,tmp_path/'client.zip','prism',client_source=str(source))
    assert result.ok and result.mod_count==2
    with zipfile.ZipFile(tmp_path/'client.zip') as z:assert not any('old-shared' in n for n in z.namelist())
    inst.minecraft_version='1.20.1'
    assert not client_export.export(inst,tmp_path/'bad.zip','prism',client_source=str(source)).ok
    assert not (tmp_path/'bad.zip').exists()


@pytest.mark.parametrize('text,kind',[('UnsupportedClassVersionError','java_version'),('Address already in use','port_in_use'),('java.lang.OutOfMemoryError','out_of_memory'),('No space left on device','disk_full'),('Invalid or corrupt jarfile','corrupt_jar')])
def test_actionable_diagnosis(text,kind):
    assert kind in [i.kind for i in parse_crash_log(text).issues]


def test_latest_log_not_permanently_shadowed_by_old_crash(tmp_path):
    crash=tmp_path/'crash-reports/crash-old.txt';crash.parent.mkdir();crash.write_text('old');os.utime(crash,(1,1))
    latest=tmp_path/'logs/latest.log';latest.parent.mkdir();latest.write_text('current')
    assert latest_crash_text(tmp_path)==('current',str(latest))


def test_real_qt_interaction_smoke():
    script=Path(__file__).with_name('qt_refinement_smoke.py')
    env={**os.environ,'QT_QPA_PLATFORM':'offscreen','PYTHONPATH':str(Path(__file__).resolve().parents[1])}
    result=subprocess.run([sys.executable,str(script)],env=env,capture_output=True,text=True,timeout=25)
    assert result.returncode==0,result.stdout+result.stderr


def test_new_install_is_staged_as_one_transaction(tmp_path,monkeypatch):
    downloader(monkeypatch)
    result=v.replace_files(tmp_path,None,[mf()])
    assert not result.failed and (tmp_path/'mods/new.jar').read_bytes()==b'new'


def test_commit_guard_keeps_original_after_late_start(tmp_path,monkeypatch):
    server(tmp_path);downloader(monkeypatch)
    def guard():raise RuntimeError('server started')
    with pytest.raises(RuntimeError):v.replace_files(tmp_path,'old.jar',[mf()],before_commit=guard)
    assert (tmp_path/'mods/old.jar').read_bytes()==b'old'
    assert not (tmp_path/'mods/new.jar').exists()


def test_changed_original_is_not_overwritten(tmp_path,monkeypatch):
    server(tmp_path)
    def download(url,dest):
        (tmp_path/'mods/old.jar').write_bytes(b'changed externally')
        return dest.write_bytes(b'new')
    monkeypatch.setattr(mr,'_download',download)
    with pytest.raises(mr.ModrinthError,match='changed'):v.replace_files(tmp_path,'old.jar',[mf()])
    assert (tmp_path/'mods/old.jar').read_bytes()==b'changed externally'


def test_conflicting_dependency_versions_refused():
    with pytest.raises(mr.ModrinthError,match='conflicting'):
        v.resolve_selected(mf(),loader='fabric',mc_version='1.21.1',_seen={'project':'different-version'})


def test_exact_identity_requires_returned_file_hash(tmp_path,monkeypatch):
    server(tmp_path);digest=hashlib.sha1(b'old').hexdigest()
    version={'project_id':'p','id':'fixed','version_number':'1','files':[{'hashes':{'sha1':digest}}]}
    def response(req,**kw):return io.BytesIO(json.dumps({digest:version}).encode())
    monkeypatch.setattr(mr.urllib.request,'urlopen',response)
    found=v.identify_files(tmp_path,[tmp_path/'mods/old.jar'])
    assert found['old.jar']['identity']=='exact-sha1'
    version['files'][0]['hashes']['sha1']='wrong'
    assert v.identify_files(tmp_path,[tmp_path/'mods/old.jar'])=={}


def test_same_downloaded_jar_links_without_redownload(tmp_path,monkeypatch):
    server(tmp_path)
    file=mf('old.jar',b'old');file.url=''
    result=mr.install_files(tmp_path,[file])
    assert not result.failed and result.skipped==['old.jar']
    assert mr.added_mods(tmp_path)['old.jar']['project_id']=='project'


def test_different_existing_download_is_not_silently_skipped(tmp_path):
    server(tmp_path)
    result=mr.install_files(tmp_path,[mf('old.jar')])
    assert result.failed and not result.skipped
    assert (tmp_path/'mods/old.jar').read_bytes()==b'old'


def test_manual_import_does_not_create_duplicate_suffix(tmp_path):
    (tmp_path/'server').mkdir();jar(tmp_path/'source/mod.jar')
    manager=ModManager(SimpleNamespace(path=str(tmp_path/'server')))
    first=manager.add_from_file(tmp_path/'source/mod.jar')
    second=manager.add_from_file(tmp_path/'source/mod.jar')
    assert first.path==second.path
    jar(tmp_path/'source/mod.jar','client')
    with pytest.raises(OSError,match='different JAR'):manager.add_from_file(tmp_path/'source/mod.jar')
    assert len(list((tmp_path/'server/mods').iterdir()))==1


def test_private_curseforge_file_loads_without_exposing_value(tmp_path,monkeypatch):
    path=tmp_path/'key';path.write_text('test-key-not-a-real-credential\n');path.chmod(0o600)
    monkeypatch.delenv('CURSEFORGE_API_KEY',raising=False);monkeypatch.delenv('CURSEFORGE_API_KEY_FILE',raising=False)
    monkeypatch.setattr(cf,'_session_key','');monkeypatch.setattr(cf,'_DEFAULT_KEY_FILE',path)
    assert cf.get_api_key()=='test-key-not-a-real-credential'
    path.chmod(0o644)
    with pytest.raises(mr.ModrinthError,match='private'):cf.get_api_key()


def test_curseforge_file_symlink_refused(tmp_path,monkeypatch):
    target=tmp_path/'target';target.write_text('test-key');target.chmod(0o600)
    path=tmp_path/'key';path.symlink_to(target)
    monkeypatch.delenv('CURSEFORGE_API_KEY',raising=False);monkeypatch.delenv('CURSEFORGE_API_KEY_FILE',raising=False)
    monkeypatch.setattr(cf,'_session_key','');monkeypatch.setattr(cf,'_DEFAULT_KEY_FILE',path)
    with pytest.raises(mr.ModrinthError,match='symlink'):cf.get_api_key()


def test_curseforge_multiline_key_refused(monkeypatch):
    monkeypatch.setattr(cf,'_session_key','line1\nline2')
    with pytest.raises(mr.ModrinthError,match='single line'):cf.get_api_key()
