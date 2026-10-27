"""No live credentials/network: verify authentication boundaries and denied installs."""
import hashlib
import io
import json
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from crucible import download_http as http
from crucible.mods import curseforge as cf, modrinth as mr, versions
from crucible.importers import downloader as packs


@pytest.fixture(autouse=True)
def isolated_key(monkeypatch):
    monkeypatch.setattr(cf, 'get_api_key', lambda: 'test-only-not-a-real-key')


@pytest.mark.parametrize('host', sorted(http._CURSEFORGE_HOSTS))
def test_key_only_attached_to_exact_official_hosts(host):
    request = http.make_request(f'https://{host}/files/test.jar')
    assert request.get_header('X-api-key') == 'test-only-not-a-real-key'


@pytest.mark.parametrize('host', ['cdn.modrinth.com', 'edge.forgecdn.net.evil.invalid',
                                  'evil-forgecdn.net', 'example.invalid'])
def test_non_curseforge_request_never_gets_key(host):
    request = http.make_request(f'https://{host}/file', headers={'X-API-KEY': 'injected'})
    assert request.get_header('X-api-key') is None


@pytest.mark.parametrize('url', ['http://edge.forgecdn.net/file',
                                 'https://user:secret@edge.forgecdn.net/file',
                                 'https://edge.forgecdn.net:444/file',
                                 'https://edge.forgecdn.net/file?api-key=secret',
                                 'https://edge.forgecdn.net/file?api_key=secret'])
def test_unsafe_url_rejected_without_echoing_credentials(url):
    with pytest.raises(http.DownloadRequestError) as caught:
        http.make_request(url)
    assert 'secret' not in str(caught.value)


def test_missing_key_fails_before_any_download(monkeypatch):
    monkeypatch.setattr(cf, 'get_api_key', lambda: '')
    with pytest.raises(http.DownloadRequestError, match='approved API key'):
        http.make_request('https://edge.forgecdn.net/files/a.jar')


@pytest.mark.parametrize('target', ['https://evil.invalid/leak',
                                    'http://edge.forgecdn.net/files/a.jar',
                                    'https://edge.forgecdn.net.evil.invalid/leak'])
def test_redirect_cannot_leak_key(target):
    request = http.make_request('https://edge.forgecdn.net/files/a.jar')
    with pytest.raises(http.DownloadRequestError):
        http.SafeRedirectHandler().redirect_request(request, None, 302, 'Found', {}, target)


def test_official_redirect_reauthenticates_only_trusted_destination():
    request = http.make_request('https://edge.forgecdn.net/files/a.jar')
    redirected = http.SafeRedirectHandler().redirect_request(
        request, None, 302, 'Found', {}, 'https://mediafilez.forgecdn.net/files/a.jar')
    assert redirected.get_header('X-api-key') == 'test-only-not-a-real-key'


def test_generic_redirect_stays_key_free():
    request = http.make_request('https://cdn.modrinth.com/a.jar')
    redirected = http.SafeRedirectHandler().redirect_request(
        request, None, 302, 'Found', {}, 'https://example.invalid/a.jar')
    assert redirected.get_header('X-api-key') is None


def response(data):
    class Response(io.BytesIO):
        headers = {}
    return Response(data)


def test_browser_installer_authenticates_cdn(monkeypatch, tmp_path):
    seen = []
    def opening(request, *, timeout):
        seen.append(request)
        return response(b'jar-content')
    monkeypatch.setattr(http, 'open_url', opening)
    mr._download('https://edge.forgecdn.net/files/a.jar', tmp_path/'a.jar')
    assert seen[0].get_header('X-api-key') == 'test-only-not-a-real-key'
    assert (tmp_path/'a.jar').read_bytes() == b'jar-content'


@pytest.mark.parametrize('code, message', [(401, 'authentication'), (403, 'denied'),
                                         (429, 'rate-limiting'), (503, 'temporarily')])
def test_failed_cdn_download_keeps_original_and_sanitizes_errors(monkeypatch, tmp_path, code, message):
    (tmp_path/'mods').mkdir()
    (tmp_path/'mods/old.jar').write_bytes(b'original')
    def denied(request, *, timeout):
        raise urllib.error.HTTPError(request.full_url, code, 'sensitive-server-detail', {}, None)
    monkeypatch.setattr(http, 'open_url', denied)
    file = mr.ModFile('cf:123', 'Test', '7', '7', 'new.jar',
                      'https://edge.forgecdn.net/files/new.jar', hashlib.sha1(b'new').hexdigest(), 3)
    result = versions.replace_files(tmp_path, 'old.jar', [file])
    assert result.failed and message in result.failed[0]
    assert 'sensitive-server-detail' not in str(result.failed)
    assert (tmp_path/'mods/old.jar').read_bytes() == b'original'
    assert not (tmp_path/'mods/new.jar').exists()
    assert not (tmp_path/'.crucible/mod-change.lock').exists()


def test_api_reject_message_is_actionable_and_redacted(monkeypatch):
    def denied(request, *, timeout):
        assert request.get_header('X-api-key') == 'test-only-not-a-real-key'
        raise urllib.error.HTTPError(request.full_url, 401, 'sensitive', {}, None)
    monkeypatch.setattr(http, 'open_url', denied)
    with pytest.raises(mr.ModrinthError, match='authentication') as caught:
        cf.search('example', loader='fabric', mc_version='1.21.1')
    assert 'sensitive' not in str(caught.value)


def manifest():
    return {'files': [{'projectID': 123, 'fileID': 456}]}


def metadata(**updates):
    data = {'fileName': 'example.jar', 'downloadUrl': 'https://edge.forgecdn.net/files/example.jar',
            'fileLength': 3, 'hashes': [{'algo': 1, 'value': hashlib.sha1(b'jar').hexdigest()}]}
    data.update(updates)
    return data


def mock_metadata(monkeypatch, data):
    monkeypatch.setattr(packs, '_opener', lambda: SimpleNamespace(
        open=lambda req, timeout: response(json.dumps({'data': data}).encode())))


def test_pack_without_key_never_attempts_public_redirect(monkeypatch):
    monkeypatch.setattr(packs, '_opener', lambda: pytest.fail('No network without key'))
    items, notes = packs._curseforge_items(manifest(), '')
    assert len(items) == 1 and not items[0].urls
    assert 'approved API key' in items[0].error
    assert notes


@pytest.mark.parametrize('updates', [{'downloadUrl': None}, {'isAvailable': False},
                                     {'downloadUrl': 'https://evil.invalid/file.jar'},
                                     {'fileName': '../escape.jar'}, {'hashes': []}])
def test_pack_resolution_never_fabricates_restricted_or_invalid_urls(monkeypatch, updates):
    mock_metadata(monkeypatch, metadata(**updates))
    items, notes = packs._curseforge_items(manifest(), 'test-only')
    assert items[0].urls == [] and items[0].error and notes


def test_pack_records_official_hash_and_authenticates_cdn(monkeypatch, tmp_path):
    mock_metadata(monkeypatch, metadata())
    items, notes = packs._curseforge_items(manifest(), 'test-only')
    assert not notes and items[0].sha1 == hashlib.sha1(b'jar').hexdigest()
    seen = []
    def opening(request, *, timeout):
        seen.append(request)
        return response(b'jar')
    opener = SimpleNamespace(open=opening)
    packs._download_one(items[0], tmp_path/'example.jar', opener, 1, None, None)
    assert seen[0].get_header('X-api-key') == 'test-only-not-a-real-key'
    assert (tmp_path/'example.jar').read_bytes() == b'jar'


def test_pack_denial_is_not_retried_or_silently_fallbacked(monkeypatch, tmp_path):
    seen = []
    def denied(request, *, timeout):
        seen.append(request)
        raise urllib.error.HTTPError(request.full_url, 401, 'sensitive', {}, None)
    item = packs.DownloadItem('x.jar', 'mods/x.jar', ['https://edge.forgecdn.net/x.jar'], source='curseforge')
    with pytest.raises(RuntimeError, match='authentication'):
        packs._download_one(item, tmp_path/'x.jar', SimpleNamespace(open=denied), 1, None, None)
    assert len(seen) == 1
    assert not (tmp_path/'x.jar').exists()
    assert not list(tmp_path.glob('.crucible-dl-*'))


def test_pack_missing_key_reports_failure_not_success(monkeypatch, tmp_path):
    monkeypatch.setattr(cf, 'get_api_key', lambda: '')
    (tmp_path/'manifest.json').write_text(json.dumps(dict(manifest(), manifestType='minecraftModpack')))
    result = packs.download_pack_mods(tmp_path)
    assert not result.downloaded and len(result.failed) == 1
    assert 'approved API key' in result.failed[0][1]
