from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import ValidationError

from urlverify_mcp.config import Config
from urlverify_mcp.identity.releases import check_release, target_from_url, annotate_result
from urlverify_mcp.models import VerifyRequest, VerifyResult, Verdict

NOW = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)
FIXTURES = yaml.safe_load((Path(__file__).parent / 'fixtures/release_cooldown.yaml').read_text())['cases']


def metadata_transport(registry, published, *, missing=False, unlisted=False):
    def handler(request):
        path = request.url.path
        if registry == 'npm':
            name = '@example/tool' if '@example' in path else 'example'
            return httpx.Response(200, json={
                'dist-tags': {'latest': '2.0.0'},
                'time': {'created': '2010-01-01T00:00:00Z', 'modified': NOW.isoformat(),
                         '1.2.3': published, '2.0.0': NOW.isoformat()},
                'versions': {'1.2.3': {'dist': {'tarball': f'https://registry.npmjs.org/{name}/-/tool-1.2.3.tgz'}}, '2.0.0': {}}})
        if registry == 'pypi':
            return httpx.Response(200, json={'info': {'name': 'example', 'version': '1.2.3'}, 'urls': [
                {'upload_time_iso_8601': published, 'url': 'https://files.pythonhosted.org/packages/example-1.2.3-py3-none-any.whl'}]})
        if path == '/v3/index.json':
            return httpx.Response(200, json={'resources': [
                {'@type': 'RegistrationsBaseUrl/3.6.0', '@id': 'https://api.nuget.org/registration/'},
                {'@type': 'PackageBaseAddress/3.0.0', '@id': 'https://api.nuget.org/content/'}]})
        if path.startswith('/content/'):
            return httpx.Response(200, json={'versions': ['1.0.0', '1.2.3', '2.0.0-beta']})
        return httpx.Response(200, json={'published': None if missing else published, 'listed': not unlisted})
    return httpx.MockTransport(handler)


@pytest.mark.parametrize('case', FIXTURES)
async def test_registry_release_fixtures(case):
    async with httpx.AsyncClient(transport=metadata_transport(case['registry'], case['published'])) as client:
        check = await check_release(case['url'], Config(), client, now=NOW)
    assert check.detail['state'] == case['state']
    assert check.detail['version'] == '1.2.3'
    assert check.detail['threshold_hours'] == 72
    assert check.status == ('warn' if case['state'] == 'active' else 'pass')
    assert check.fatal is False


@pytest.mark.parametrize('url,registry,name,version', [
    ('https://www.npmjs.com/package/foo/v/1.2.3', 'npm', 'foo', '1.2.3'),
    ('https://registry.npmjs.org/@scope%2Ffoo/1.2.3', 'npm', '@scope/foo', '1.2.3'),
    ('https://pypi.org/pypi/foo/1.2.3/json', 'pypi', 'foo', '1.2.3'),
    ('https://www.nuget.org/api/v2/package/Example/1.2.3', 'nuget', 'Example', '1.2.3'),
    ('https://www.nuget.org/packages/Example', 'nuget', 'Example', None),
])
def test_target_parsing(url, registry, name, version):
    target = target_from_url(url)
    assert (target.registry, target.name, target.version) == (registry, name, version)
    assert target_from_url('https://nuget.org.evil.example/packages/Example') is None


async def test_disabled_does_not_query_and_unrelated_target_is_skipped():
    def forbidden(request):
        raise AssertionError('network must not run')
    cfg = Config(release_cooldown={'hours': 0})
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        for case in FIXTURES:
            check = await check_release(case['url'], cfg, client)
            assert check.detail['state'] == 'disabled'
        assert await check_release('https://example.com', cfg, client) is None


@pytest.mark.parametrize('hours', [-1, float('inf'), float('nan')])
def test_invalid_config(hours):
    with pytest.raises(ValidationError):
        Config(release_cooldown={'hours': hours})


@pytest.mark.parametrize('stamp', [None, '1900-01-01T00:00:00Z', '2027-01-01T00:00:00Z', '2026-09-01', 'broken'])
async def test_unknown_timestamps_never_pass(stamp):
    async with httpx.AsyncClient(transport=metadata_transport('nuget', stamp)) as client:
        check = await check_release('https://www.nuget.org/packages/Example/1.2.3', Config(), client, now=NOW)
    assert check.detail['state'] == 'unknown'
    assert check.status == 'error'


async def test_exact_boundary_and_fractional_hours():
    cfg = Config(release_cooldown={'hours': 0.5})
    async with httpx.AsyncClient(transport=metadata_transport('npm', '2026-09-19T11:30:00Z')) as client:
        check = await check_release('https://registry.npmjs.org/example/1.2.3', cfg, client, now=NOW)
    assert check.detail['state'] == 'elapsed'
    assert check.detail['remaining_hours'] == 0


@pytest.mark.parametrize('registry,url,expected', [
    ('npm', 'https://www.npmjs.com/package/example', '2.0.0'),
    ('pypi', 'https://pypi.org/project/example/', '1.2.3'),
    ('nuget', 'https://www.nuget.org/packages/Example', '1.2.3'),
])
async def test_unversioned_resolves_and_names_current_version(registry, url, expected):
    async with httpx.AsyncClient(transport=metadata_transport(registry, NOW.isoformat())) as client:
        check = await check_release(url, Config(), client, now=NOW)
    assert check.detail['version'] == expected
    assert check.detail['resolution'].startswith('latest')
    assert check.detail['checked_at'] == NOW.isoformat()


async def test_pypi_new_wheel_restarts_version_window_but_not_old_artifact():
    old_url = 'https://files.pythonhosted.org/packages/example-1.2.3-py3-none-any.whl'
    def handler(request):
        return httpx.Response(200, json={'info': {'name': 'example', 'version': '1.2.3'}, 'urls': [
            {'url': old_url, 'upload_time_iso_8601': '2026-01-01T00:00:00Z'},
            {'url': 'https://files.pythonhosted.org/packages/new.whl', 'upload_time_iso_8601': NOW.isoformat()}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await check_release('https://pypi.org/project/example/1.2.3/', Config(), client, now=NOW)
        artifact = await check_release(old_url, Config(), client, now=NOW)
        fake = await check_release(old_url.replace('/packages/', '/wrong/'), Config(), client, now=NOW)
    assert page.detail['state'] == 'active'
    assert artifact.detail['state'] == 'elapsed'
    assert fake.detail['state'] == 'unknown'


async def test_tarball_matches_metadata_and_not_filename_guess():
    async with httpx.AsyncClient(transport=metadata_transport('npm', '2026-01-01T00:00:00Z')) as client:
        check = await check_release('https://registry.npmjs.org/@example/tool/-/tool-1.2.3.tgz', Config(), client, now=NOW)
    assert check.detail['state'] == 'elapsed'
    assert check.detail['version'] == '1.2.3'


async def test_failed_registry_unknown_and_no_untrusted_redirects():
    for status in (404, 503, 302):
        calls = []
        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(status, headers={'Location': 'http://127.0.0.1/private'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            check = await check_release('https://pypi.org/project/example/', Config(), client, now=NOW)
        assert check.detail['state'] == 'unknown'
        assert len(calls) == 1


@pytest.mark.parametrize('verdict', list(Verdict))
def test_advisory_preserves_verdict_confidence_and_language(verdict):
    result = VerifyResult(verdict=verdict, confidence=0.75, reason='來源判定', checks={'release_cooldown': {'detail': {
        'state': 'active', 'version': '1.2.3', 'age_hours': 12, 'threshold_hours': 72, 'remaining_hours': 60}}})
    annotate_result(result, VerifyRequest(project='Example', url='https://example.com', description='安裝套件'))
    assert result.verdict == verdict and result.confidence == 0.75
    assert '冷卻期' in result.reason
    assert 'RELEASE_COOLDOWN_PERIOD:12' in result.risk_signals
    assert '必須先向使用者說明風險並取得確認' in result.reason


async def test_nuget_unlisted_latest_and_hostile_service_endpoint():
    async with httpx.AsyncClient(transport=metadata_transport('nuget', NOW.isoformat(), unlisted=True)) as client:
        check = await check_release('https://www.nuget.org/packages/Example', Config(), client, now=NOW)
    assert check.detail['state'] == 'unknown'
    calls = []
    def hostile(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={'resources': [
            {'@type': 'RegistrationsBaseUrl/3.6.0', '@id': 'http://127.0.0.1/private'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(hostile)) as client:
        check = await check_release('https://www.nuget.org/packages/Example/1.2.3', Config(), client, now=NOW)
    assert check.detail['state'] == 'unknown'
    assert calls == ['https://api.nuget.org/v3/index.json']


async def test_nuget_l1_registry_tool():
    from urlverify_mcp.identity.structured import Structured
    from urlverify_mcp.agent.loop import Investigator
    def handler(request):
        if request.url.path == '/v3/index.json':
            return httpx.Response(200, json={'resources': [
                {'@type': 'RegistrationsBaseUrl/3.6.0', '@id': 'https://api.nuget.org/registration/'},
                {'@type': 'PackageBaseAddress/3.0.0', '@id': 'https://api.nuget.org/content/'}]})
        if request.url.path == '/content/example/index.json':
            return httpx.Response(200, json={'versions': ['1.2.3']})
        if request.url.path == '/catalog/entry.json':
            return httpx.Response(200, json={'id': 'Example', 'authors': 'Example Team', 'projectUrl': 'https://example.com'})
        return httpx.Response(200, json={'published': NOW.isoformat(), 'listed': True, 'catalogEntry': 'https://api.nuget.org/catalog/entry.json'})
    structured = Structured(1, 'test')
    await structured.close()
    structured.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        inv = Investigator(Config(), None, None, structured)
        out = await inv.run_tool('package_registry', {'registry': 'nuget', 'name': 'Example'})
        assert 'Example Team' in out and 'https://example.com' in out
        assert 'https://api.nuget.org/registration/example/1.2.3.json' in inv.evidence_store
    finally:
        await structured.close()


@pytest.mark.parametrize('mode,registry,fast_result', [
    ('auto', 'npm', True), ('quick', 'npm', True), ('quick', 'npm', False),
    ('full', 'npm', False), ('full', 'pypi', False), ('full', 'nuget', False),
])
async def test_pipeline_modes_cache_and_history(monkeypatch, tmp_path, mode, registry, fast_result):
    from urlverify_mcp import pipeline
    from urlverify_mcp.models import CheckResult, L0Result, LLMSubmission, IdentityGraph
    from urlverify_mcp.storage import Storage
    from urlverify_mcp.rules import Decision
    import urlverify_mcp.promptstore as ps

    monkeypatch.setattr(ps, '_store', None)
    cfg = Config(storage={'dir': str(tmp_path / 'state')}, log={'dir': str(tmp_path / 'log')},
                 prompts={'dir': str(tmp_path / 'prompts')}, package_registry_fast_path={'mode': mode})
    store = Storage(cfg.storage.resolved(), cfg.log.resolved())
    store.put_identity('Example', {'official_orgs': {registry: ['example']}, 'policy_fingerprint': pipeline.identity_policy(cfg)}, 3600)
    cached_before = store._map('identity_cache')
    url = {'npm': 'https://www.npmjs.com/package/example/v/1.2.3',
           'pypi': 'https://pypi.org/project/example/1.2.3/',
           'nuget': 'https://www.nuget.org/packages/Example/1.2.3'}[registry]
    host = url.split('/')[2]
    async def l0(*args, **kwargs):
        return L0Result(normalized_url=url, host=host, etld1=host.removeprefix('www.'),
                        platform=registry, platform_owner='example',
                        checks=[CheckResult(name='tls', status='pass')])
    class Provider:
        async def fetch(self, url): return 'Example release'
        async def close(self): pass
    class Structured:
        def __init__(self, *args):
            self.client = httpx.AsyncClient(transport=metadata_transport(registry, datetime.now(timezone.utc).isoformat()))
        async def close(self): await self.client.aclose()
    class Investigator:
        def __init__(self, *args):
            self.evidence_store = {}
            self.target_page_text = None
        async def investigate(self, *args):
            return LLMSubmission(identity=IdentityGraph(product='Example'))
    class Fast:
        def __init__(self, *args): pass
        async def run(self, l0, t0, trace_id, project):
            if not fast_result: return None
            return VerifyResult(verdict=Verdict.TRUE, confidence=0.8, reason='source verified', path='registry_fast_path', trace_id=trace_id,
                                checks={c.name: c.model_dump(exclude={'name'}) for c in l0.checks})
        async def registry_state(self, *args): return {'state': 'ok'}
    async def reason(*args): return '已確認來源'
    monkeypatch.setattr(pipeline, 'run_l0', l0)
    monkeypatch.setattr(pipeline, 'LLM', lambda *args: None)
    monkeypatch.setattr(pipeline, 'make_search_provider', lambda *args: Provider())
    monkeypatch.setattr(pipeline, 'make_fetcher', lambda *args: Provider())
    monkeypatch.setattr(pipeline, 'Structured', Structured)
    monkeypatch.setattr(pipeline, 'Investigator', Investigator)
    monkeypatch.setattr(pipeline, 'RegistryFastPath', Fast)
    monkeypatch.setattr(pipeline, '_write_reason', reason)
    monkeypatch.setattr(pipeline, 'decide', lambda *args: Decision(Verdict.TRUE, 0.8, established_orgs={registry: ['example']}))
    request = VerifyRequest(project='Example', url=url, description='安裝套件')
    result = await pipeline.verify(request, cfg, store)
    assert result.checks['release_cooldown']['detail']['state'] == 'active'
    assert '冷卻期' in result.reason and any(s.startswith('RELEASE_COOLDOWN_PERIOD:') for s in result.risk_signals)
    if mode == 'quick' and not fast_result:
        assert result.verdict == Verdict.UNVERIFIABLE
    else:
        assert result.verdict == Verdict.TRUE and result.confidence == 0.8
    assert 'identity' in result.cache_hits
    assert store._map('identity_cache') == cached_before  # cache hits must not renew the original expiry
    saved = store.get_history(result.trace_id)['result']
    assert saved['reason'] == result.reason
    assert saved['checks']['release_cooldown'] == result.checks['release_cooldown']
    assert len((tmp_path / 'log/history/index.jsonl').read_text().splitlines()) == 1


@pytest.mark.parametrize('age,label', [(0, '0'), (36, '36'), (36.5, '36.5'), (71.999999, '71.999999')])
@pytest.mark.parametrize('description', ['安裝套件', 'Install package'])
def test_cooldown_signal_elapsed_hours_and_confirmation(age, label, description):
    result = VerifyResult(verdict=Verdict.TRUE, confidence=0.8, reason='verified', checks={'release_cooldown': {'detail': {
        'state': 'active', 'version': '1.0', 'age_hours': age, 'threshold_hours': 72, 'remaining_hours': 72-age}}})
    annotate_result(result, VerifyRequest(project='Example', url='https://example.com', description=description))
    assert result.risk_signals == [f'RELEASE_COOLDOWN_PERIOD:{label}']
    assert result.verdict == Verdict.TRUE and result.confidence == 0.8
    assert ('取得確認' if description == '安裝套件' else 'obtain confirmation') in result.reason


@pytest.mark.parametrize('state', ['unknown', 'elapsed', 'disabled'])
def test_other_cooldown_states_do_not_emit_elapsed_hours(state):
    result = VerifyResult(verdict=Verdict.TRUE, confidence=0.8, reason='verified',
                          checks={'release_cooldown': {'detail': {'state': state}}})
    annotate_result(result, VerifyRequest(project='Example', url='https://example.com', description='Install package'))
    if state == 'unknown':
        assert result.risk_signals == ['release_cooldown_unknown']
        assert 'obtain confirmation' in result.reason
    else:
        assert result.risk_signals == [] and result.reason == 'verified'
