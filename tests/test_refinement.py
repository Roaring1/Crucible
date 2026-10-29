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
from crucible.diagnostics.loadcheck import autofix_loading, parse_crash_log, latest_crash_text


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


def test_curseforge_versions_filter_explicit_minecraft_mismatch(monkeypatch):
    routes = []
    def fake_get(route):
        routes.append(route)
        return [
            {'id': 1, 'fileName': 'compatible.jar', 'displayName': '1.20.1',
             'downloadUrl': 'https://edge.forgecdn.net/compatible.jar',
             'gameVersions': ['1.20.1'], 'hashes': [{'algo': 1, 'value': 'a' * 40}]},
            {'id': 2, 'fileName': 'wrong-mc.jar', 'displayName': '1.19.4',
             'downloadUrl': 'https://edge.forgecdn.net/wrong.jar',
             'gameVersions': ['1.19.4'], 'hashes': [{'algo': 1, 'value': 'b' * 40}]},
        ]
    monkeypatch.setattr(cf, '_get', fake_get)
    versions = v.list_versions('cf:123', loader='forge', mc_version='1.20.1')
    assert [item.filename for item in versions] == ['compatible.jar']
    assert versions[0].project_id == 'cf:123'
    assert 'gameVersion=1.20.1' in routes[0] and 'modLoaderType=1' in routes[0]


def test_resolve_selected_keeps_curseforge_required_dependency_provider_ids(monkeypatch):
    main = mr.ModFile('cf:123', 'Main', '100', '1.0', 'main.jar',
                      'https://edge.forgecdn.net/main.jar', 'a' * 40, 10,
                      dependencies=['cf:456'])
    dependency = mr.ModFile('cf:456', 'Dependency', '200', '2.0', 'dep.jar',
                            'https://edge.forgecdn.net/dep.jar', 'b' * 40, 20)
    calls = []
    def fake_list(project_id, *, loader, mc_version):
        calls.append(project_id)
        return [dependency] if project_id == 'cf:456' else []
    monkeypatch.setattr(v, 'list_versions', fake_list)
    resolved = v.resolve_selected(main, loader='forge', mc_version='1.20.1')
    assert [item.project_id for item in resolved] == ['cf:123', 'cf:456']
    assert calls == ['cf:456']


def test_curseforge_identified_project_link_resolves_official_slug(monkeypatch):
    routes = []
    monkeypatch.setattr(cf, '_get', lambda route: routes.append(route) or {'id': 123, 'slug': 'example-mod'})
    assert cf.project_url('cf:123') == 'https://www.curseforge.com/minecraft/mc-mods/example-mod'
    assert routes == ['/mods/123']
    with pytest.raises(mr.ModrinthError, match='Invalid CurseForge project ID'):
        cf.project_url('cf:not-an-id')


def test_curseforge_version_keeps_missing_download_url(monkeypatch):
    monkeypatch.setattr(cf,'_get',lambda _: [{'id':7,'fileName':'a.jar','downloadUrl':None,'gameVersions':['1.20.1'],'hashes':[{'algo':1,'value':'a'*40}]}])
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
    server=tmp_path/'server';jar(server/'mods/shared.jar','*',name='shared');jar(server/'mods/server.jar','server',name='server');jar(server/'mods/client.jar','client',name='client')
    (server/'world').mkdir();(server/'world/secret.dat').write_text('not for client')
    inst=SimpleNamespace(path=str(server),name='Example',minecraft_version='1.21.1',loader='fabric',loader_version='0.16.10')
    for fmt,suffix in [('prism','zip'),('mrpack','mrpack'),('curseforge','zip')]:
        out=tmp_path/f'client-{fmt}.{suffix}';result=client_export.export(inst,out,fmt)
        assert result.ok and result.mod_count==2
        with zipfile.ZipFile(out) as z:
            assert z.testzip() is None
            assert not any('world/' in n or n.endswith('server.jar') for n in z.namelist())


def test_client_source_adds_only_declared_client_extras(tmp_path):
    server=tmp_path/'server';jar(server/'mods/shared.jar','*',name='shared')
    source=tmp_path/'prism';jar(source/'.minecraft/mods/client.jar','client',name='client');jar(source/'.minecraft/mods/old-shared.jar','*',name='shared')
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


def test_progresspeek_glfw_bootstrap_crash_is_detected_and_quarantined(tmp_path):
    server_dir = tmp_path / "server"
    mods = server_dir / "mods"
    mods.mkdir(parents=True)
    jar_path = mods / "progresspeek-1.0.1+1.21.1-neoforge.jar"
    with zipfile.ZipFile(jar_path, "w") as z:
        # This mirrors the mod's misleading side=BOTH loader metadata.
        z.writestr("META-INF/neoforge.mods.toml", '[[mods]]\nmodId = "progresspeek"\n')

    log = '''Exception in thread "main" java.lang.NoClassDefFoundError: org/lwjgl/glfw/GLFWErrorCallbackI
        at LAYER SERVICE/me.decce.progresspeek.neoforge@1.0.1+1.21.1-neoforge/me.decce.progresspeek.service.Bootstrapper.bootstrap(Bootstrapper.java:42)
        at LAYER SERVICE/me.decce.progresspeek.neoforge@1.0.1+1.21.1-neoforge/me.decce.progresspeek.service.neoforge.GraphicsBootstrapperImpl.bootstrap(GraphicsBootstrapperImpl.java:16)
'''
    result = autofix_loading(server_dir, apply=True, log_text=log)
    assert result.diagnosis.client_on_server_modids == ["progresspeek"]
    assert result.quarantined == [jar_path.name]
    assert (mods / (jar_path.name + ".disabled")).is_file()
    assert not jar_path.exists()


def test_latest_startup_failure_output_is_used_for_diagnosis(tmp_path):
    server_dir = tmp_path / "server"
    mods = server_dir / "mods"
    mods.mkdir(parents=True)
    jar_path = mods / "progresspeek-1.0.1+1.21.1-neoforge.jar"
    with zipfile.ZipFile(jar_path, "w") as z:
        z.writestr("META-INF/neoforge.mods.toml", '[[mods]]\nmodId = "progresspeek"\n')
    logs = server_dir / "logs"
    logs.mkdir()
    latest = logs / "latest.log"
    latest.write_text("ModLauncher starting\n")
    startup = server_dir / ".crucible" / "startup-failure.log"
    startup.parent.mkdir()
    startup.write_text('''Exception in thread "main" java.lang.NoClassDefFoundError: org/lwjgl/glfw/GLFWErrorCallbackI
        at LAYER SERVICE/me.decce.progresspeek.neoforge@1.0.1+1.21.1-neoforge/me.decce.progresspeek.service.Bootstrapper.bootstrap(Bootstrapper.java:42)
        at LAYER SERVICE/me.decce.progresspeek.neoforge@1.0.1+1.21.1-neoforge/me.decce.progresspeek.service.neoforge.GraphicsBootstrapperImpl.bootstrap(GraphicsBootstrapperImpl.java:16)
''')
    os.utime(latest, (1, 1))
    text, source = latest_crash_text(server_dir)
    assert source == str(startup)
    assert "GLFWErrorCallbackI" in text

    result = autofix_loading(server_dir, apply=True)
    assert result.diagnosis.client_on_server_modids == ["progresspeek"]
    assert result.quarantined == [jar_path.name]
    assert (mods / (jar_path.name + ".disabled")).is_file()


def test_latest_log_not_permanently_shadowed_by_old_crash(tmp_path):
    crash=tmp_path/'crash-reports/crash-old.txt';crash.parent.mkdir();crash.write_text('old');os.utime(crash,(1,1))
    latest=tmp_path/'logs/latest.log';latest.parent.mkdir();latest.write_text('current')
    assert latest_crash_text(tmp_path)==('current',str(latest))


def test_real_qt_interaction_smoke(tmp_path):
    script=Path(__file__).with_name('qt_refinement_smoke.py')
    # Tests must not consume a developer's real API key or depend on online
    # CurseForge responses; API integration is tested explicitly elsewhere.
    env={**os.environ,'QT_QPA_PLATFORM':'offscreen','PYTHONPATH':str(Path(__file__).resolve().parents[1]),
         'CURSEFORGE_API_KEY_FILE':str(tmp_path/'no-curseforge-key')}
    env.pop('CURSEFORGE_API_KEY', None)
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


def test_curseforge_fingerprint_matches_reference_vector_and_normalizes_whitespace(tmp_path):
    # Fixed regression vector cross-checked against a compatible Murmur2/seed-1 implementation.
    assert cf.fingerprint_bytes(b'foo') == 197930586
    assert cf.fingerprint_bytes(b'a\tb\n c\r') == cf.fingerprint_bytes(b'abc')
    path = tmp_path / 'large-boundary.jar'
    data = (b'abc def\tghi\rjkl\n' * 1200) + b'end'
    path.write_bytes(data)
    assert cf.fingerprint_file(path, chunk_size=7) == cf.fingerprint_bytes(data)


def test_curseforge_exact_fingerprint_lookup_uses_authenticated_official_endpoint(monkeypatch):
    from crucible import download_http
    fingerprint = 197930586
    body = {
        'data': {
            'exactMatches': [{
                'id': fingerprint,
                'file': {
                    'id': 456, 'modId': 123, 'fileFingerprint': fingerprint,
                    'displayName': 'Demo release', 'fileName': 'demo.jar',
                    'hashes': [{'algo': 1, 'value': 'a' * 40}],
                },
            }],
            # Fuzzy/partial matches must never become identity.
            'partialMatches': [{'id': 999, 'file': {'id': 888, 'modId': 777}}],
        }
    }
    captured = {}
    monkeypatch.setattr(cf, 'has_api_key', lambda: True)
    monkeypatch.setattr(cf, 'get_api_key', lambda: 'synthetic-test-key')
    def fake_open(request, *, timeout):
        captured['url'] = request.full_url
        captured['method'] = request.get_method()
        captured['body'] = json.loads(request.data)
        captured['key'] = request.get_header('X-api-key')
        return io.BytesIO(json.dumps(body).encode())
    monkeypatch.setattr(download_http, 'open_url', fake_open)
    found = cf.identify_fingerprints({fingerprint: [Path('demo.jar')]})
    assert captured['url'] == 'https://api.curseforge.com/v1/fingerprints/432'
    assert captured['method'] == 'POST'
    assert captured['body'] == {'fingerprints': [fingerprint]}
    assert captured['key'] == 'synthetic-test-key'
    assert found[fingerprint]['project_id'] == 'cf:123'
    assert found[fingerprint]['version_id'] == '456'
    assert found[fingerprint]['identity'] == 'exact-curseforge-fingerprint'
    assert set(found) == {fingerprint}


def test_curseforge_fingerprint_lookup_rejects_inconsistent_exact_match(monkeypatch):
    from crucible import download_http
    fingerprint = 197930586
    payload = {'data': {'exactMatches': [{
        'id': fingerprint,
        'file': {'id': 1, 'modId': 2, 'fileFingerprint': fingerprint + 1,
                 'displayName': 'inconsistent', 'fileName': 'x.jar'},
    }]}}
    monkeypatch.setattr(cf, 'has_api_key', lambda: True)
    monkeypatch.setattr(cf, 'get_api_key', lambda: 'synthetic-test-key')
    monkeypatch.setattr(download_http, 'open_url', lambda request, timeout: io.BytesIO(json.dumps(payload).encode()))
    assert cf.identify_fingerprints({fingerprint: [Path('x.jar')]}) == {}


def test_identify_files_uses_curseforge_when_key_available_and_preserves_duplicate_jars(tmp_path, monkeypatch):
    server(tmp_path)
    (tmp_path / 'mods' / 'copy.jar').write_bytes(b'old')
    monkeypatch.setattr(mr.urllib.request, 'urlopen', lambda req, **kw: io.BytesIO(b'{}'))
    monkeypatch.setattr(cf, 'has_api_key', lambda: True)
    monkeypatch.setattr(cf, 'fingerprint_file', lambda path: 1234)
    monkeypatch.setattr(cf, 'identify_fingerprints', lambda mapping: {
        1234: {'project_id': 'cf:321', 'version_id': '654', 'version': 'Demo',
               'fingerprint': 1234, 'identity': 'exact-curseforge-fingerprint', 'sha1': ''}
    })
    found = v.identify_files(tmp_path, [tmp_path/'mods/old.jar', tmp_path/'mods/copy.jar'])
    assert found['old.jar']['project_id'] == 'cf:321'
    assert found['copy.jar']['project_id'] == 'cf:321'
    record = json.loads((tmp_path/'.crucible/added-mods.json').read_text())
    assert record['old.jar']['identity'] == 'exact-curseforge-fingerprint'
    assert record['copy.jar']['identity'] == 'exact-curseforge-fingerprint'


def test_identify_files_keeps_modrinth_available_without_curseforge_key(tmp_path, monkeypatch):
    server(tmp_path)
    digest = hashlib.sha1(b'old').hexdigest()
    version = {'project_id': 'mr-project', 'id': 'mr-version', 'version_number': '1',
               'files': [{'hashes': {'sha1': digest}}]}
    monkeypatch.setattr(mr.urllib.request, 'urlopen', lambda req, **kw: io.BytesIO(json.dumps({digest: version}).encode()))
    monkeypatch.setattr(cf, 'has_api_key', lambda: False)
    found = v.identify_files(tmp_path, [tmp_path/'mods/old.jar'])
    assert found['old.jar']['identity'] == 'exact-sha1'
    assert found['old.jar']['project_id'] == 'mr-project'


def test_identify_files_keeps_modrinth_exact_matches_when_curseforge_lookup_fails(tmp_path, monkeypatch):
    server(tmp_path)
    digest = hashlib.sha1(b'old').hexdigest()
    version = {'project_id': 'mr-project', 'id': 'mr-version', 'version_number': '1',
               'files': [{'hashes': {'sha1': digest}}]}
    monkeypatch.setattr(mr.urllib.request, 'urlopen', lambda req, **kw: io.BytesIO(json.dumps({digest: version}).encode()))
    monkeypatch.setattr(cf, 'has_api_key', lambda: True)
    monkeypatch.setattr(cf, 'fingerprint_file', lambda path: 1234)
    monkeypatch.setattr(cf, 'identify_fingerprints', lambda mapping: (_ for _ in ()).throw(mr.ModrinthError('CurseForge key rejected (HTTP 403).')))

    found = v.identify_files(tmp_path, [tmp_path/'mods/old.jar'])

    assert found['old.jar']['identity'] == 'exact-sha1'
    assert found['old.jar']['project_id'] == 'mr-project'
    assert 'CurseForge key rejected' in found.warning
    record = json.loads((tmp_path/'.crucible/added-mods.json').read_text())
    assert record['old.jar']['project_id'] == 'mr-project'


def test_identify_files_still_surfaces_curseforge_error_without_any_modrinth_match(tmp_path, monkeypatch):
    server(tmp_path)
    monkeypatch.setattr(mr.urllib.request, 'urlopen', lambda req, **kw: io.BytesIO(b'{}'))
    monkeypatch.setattr(cf, 'has_api_key', lambda: True)
    monkeypatch.setattr(cf, 'fingerprint_file', lambda path: 1234)
    monkeypatch.setattr(cf, 'identify_fingerprints', lambda mapping: (_ for _ in ()).throw(mr.ModrinthError('CurseForge key rejected (HTTP 403).')))

    with pytest.raises(mr.ModrinthError, match='key rejected'):
        v.identify_files(tmp_path, [tmp_path/'mods/old.jar'])
    assert not (tmp_path/'.crucible/added-mods.json').exists()


def test_curseforge_file_replace_records_namespaced_identity_and_can_list_versions(tmp_path, monkeypatch):
    server(tmp_path)
    data = b'curseforge jar bytes'
    digest = hashlib.sha1(data).hexdigest()
    file = mr.ModFile('cf:321', 'Example', '654', 'Example release', 'example.jar',
                      'https://edge.forgecdn.net/files/example.jar', digest, len(data))
    monkeypatch.setattr(mr, '_download', lambda url, dest: (dest.write_bytes(data) and len(data)))
    result = v.replace_files(tmp_path, None, [file])
    assert not result.failed
    record = mr.added_mods(tmp_path)['example.jar']
    assert record['project_id'] == 'cf:321' and record['version_id'] == '654'

    routes = []
    monkeypatch.setattr(cf, '_get', lambda route: routes.append(route) or [{
        'id': 654, 'fileName': 'example.jar', 'displayName': 'Example release',
        'downloadUrl': 'https://edge.forgecdn.net/files/example.jar',
        'gameVersions': ['1.20.1'], 'hashes': [{'algo': 1, 'value': digest}],
    }])
    versions = v.list_versions(record['project_id'], loader='forge', mc_version='1.20.1')
    assert versions[0].project_id == 'cf:321' and versions[0].version_id == '654'
    assert routes and '/mods/321/files?' in routes[0]


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
