"""Offline test seam for new OpenLeads crates.io source; no network access."""
from openleads.models import Query
from openleads.sources.crates import CratesSource, _domain_for_homepage, _parse_owners


def _crate(name, homepage='', repository=''):
    return {'name': name, 'homepage': homepage, 'repository': repository}


def _owner(login, name=None):
    return {'login': login, 'name': name}


def test_pure_parse_excludes_teams_handles_bad_shapes_and_no_guessing():
    crate = _crate('fastcsv', homepage='https://fastcsv.dev/docs',
                   repository='https://github.com/acme/fastcsv')
    rows = _parse_owners(crate, {'users': [
        _owner('github:rust-lang:core', 'Core Team'),
        _owner('rust-alice', 'Alice Smith'),
        _owner('nobody', None),
        {'login': None, 'name': 'Unknown'},
        'broken',
    ]})
    assert len(rows) == 1
    ent = rows[0]
    assert ent.full_name == 'Alice Smith'
    assert ent.domain == 'fastcsv.dev'
    assert ent.organization == 'fastcsv'
    assert ent.extra['domain_evidence'] == 'crate_homepage_not_owner_email'
    assert ent.extra['public_email'] == ''
    assert ent.links['crates'] == 'https://crates.io/crates/fastcsv'
    assert ent.source == 'crates'
    assert _parse_owners({}, {'users': [_owner('joe', 'Joe Smith')]}) == []
    assert _parse_owners(crate, {'users': None}) == []
    assert _parse_owners(crate, []) == []


def test_homepage_domain_excludes_shared_hosts_and_bad_uris():
    for bad in ('', 'https://github.com/acme/repo', 'https://foo.github.io/',
                'https://crates.io/crates/foo', 'https://docs.rs/foo',
                'file:///etc/passwd', 'javascript:evil()', 'https://localhost/',
                'https://127.0.0.1/x', 'https://evil.com@github.com/repo',
                'https://malicious.dev:bad-port/path'):
        assert _domain_for_homepage(bad) == '', bad
    assert _domain_for_homepage('https://WWW.Data.Tools.example/path') == 'data.tools.example'


def test_search_uses_identifying_ua_shared_cache_no_retries_and_cap(monkeypatch):
    calls = []
    def fake_json(url, **kw):
        calls.append((url, kw))
        if url.endswith('/owners'):
            return {'users': [_owner('alice', 'Alice Smith'), _owner('alice', 'Alice Smith')]}
        return {'crates': [_crate('crate-'+str(i), 'https://tools.example') for i in range(20)]}
    monkeypatch.setattr('openleads.sources.crates.get_json', fake_json)
    fake_time = [0.0]
    sleeps = []
    monkeypatch.setattr('openleads.sources.crates._clock', lambda: fake_time[0])
    def fake_sleep(s):
        assert s >= 0
        sleeps.append(s)
        fake_time[0] += s
    monkeypatch.setattr('openleads.sources.crates._sleep', fake_sleep)
    monkeypatch.setattr('openleads.sources.crates._next_start', 0.0)
    src = CratesSource()
    src.cache = object()
    result = list(src.search(Query(keyword='data tools', count=2, max_companies=2)))
    assert len(result) == 1  # same public user across two crates, unique owner
    assert len(calls) == 3    # one query + 2 owner calls
    assert all('retries' not in c[1] for c in calls)
    assert all(c[1]['ttl_ns'] == 'dataset' for c in calls)
    assert all(c[1]['cache'] is src.cache for c in calls)
    assert all('openleads' in c[1]['headers']['User-Agent'].lower() for c in calls)
    assert any('data%20tools' in c[0] for c in calls)
    assert sleeps == [1.05, 1.05]


def test_failure_is_partial_not_fatal(monkeypatch):
    def fake_json(url, **kw):
        if '/crates?' in url:
            return {'crates': [_crate('first', 'https://first.example'),
                               _crate('second', 'https://second.example')]}
        if '/first/owners' in url:
            return None
        return {'users': [_owner('bob', 'Bob Builder')]}
    monkeypatch.setattr('openleads.sources.crates.get_json', fake_json)
    monkeypatch.setattr('openleads.sources.crates._clock', lambda: 0.0)
    monkeypatch.setattr('openleads.sources.crates._sleep', lambda s: None)
    monkeypatch.setattr('openleads.sources.crates._next_start', 0.0)
    rows = list(CratesSource().search(Query(keyword='tools', count=4)))
    assert len(rows) == 1
    assert rows[0].organization == 'second'


def test_empty_and_invalid_searches_are_no_network(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError('unexpected fetch')
    monkeypatch.setattr('openleads.sources.crates.get_json', unexpected)
    for query in (Query(keyword='', count=5), Query(keyword='   ', count=5),
                  Query(keyword='x', count=0)):
        assert list(CratesSource().search(query)) == []


def test_unsafe_crate_id_and_unknown_owners_ignored(monkeypatch):
    requests = []
    def fake_json(url,**kw):
        requests.append(url)
        if '/crates?' in url:
            return {'crates': [_crate('bad/../path'),
                               _crate('good-crate', 'https://github.com/abc')]}
        return {'users': [_owner('hello', 'Hello World')]}
    monkeypatch.setattr('openleads.sources.crates.get_json', fake_json)
    monkeypatch.setattr('openleads.sources.crates._clock', lambda: 0.0)
    monkeypatch.setattr('openleads.sources.crates._sleep', lambda s: None)
    monkeypatch.setattr('openleads.sources.crates._next_start', 0.0)
    rows = list(CratesSource().search(Query(keyword='test',count=5)))
    assert len(rows) == 1
    assert rows[0].organization == 'good-crate'
    assert rows[0].domain == ''
    assert not any('bad/' in url for url in requests)


def test_source_does_not_use_location_to_claim_filter(monkeypatch):
    urls = []
    def fake_json(url,**kw):
        urls.append(url)
        return {'crates': []}
    monkeypatch.setattr('openleads.sources.crates.get_json',fake_json)
    list(CratesSource().search(Query(keyword='k8s',count=2)))
    assert all('k8s' in u for u in urls)


def test_private_and_numeric_alias_homepage_domains_are_rejected():
    # crates.io records are external data; avoid passing private hostnames
    # or browser-recognized numeric loopback aliases into enrichment.
    for homepage in (
        "https://metadata.google.internal/latest/meta-data/",
        "http://router.local/",
        "http://127.1/",
        "http://0x7f.1/",
        "http://0177.0.0.1/",
        "https://api.lan/",
        "https://ops.private.localhost/",
    ):
        assert _domain_for_homepage(homepage) == "", homepage
    assert _domain_for_homepage("https://www.public-project.dev/docs") == "public-project.dev"

def test_crate_website_ignores_unsafe_homepage_and_repository():
    owner_response = {'users': [_owner('alice', 'Alice Smith')]}
    crate = _crate('fastcsv', 'http://127.0.0.1/admin',
                   'https://github.com/acme/fastcsv')
    row, = _parse_owners(crate, owner_response)
    assert row.website == 'https://github.com/acme/fastcsv'
    assert row.domain == ''

    for private_url in (
        'http://127.0.0.1/secrets', 'https://127.1/secret',
        'http://169.254.169.254/latest/meta-data/',
        'https://service.internal/ops', 'http://router.local/',
        'file:///etc/passwd', 'javascript:alert(1)',
        'https://user:password@github.com/acme/fastcsv',
    ):
        unsafe = _crate('fastcsv', private_url, 'http://localhost/repo')
        rows = _parse_owners(unsafe, owner_response)
        assert len(rows) == 1
        assert rows[0].website == 'https://crates.io/crates/fastcsv', private_url
        assert rows[0].domain == ''


def test_crate_website_keeps_public_project_homepages_and_codehosts():
    owner_response = {'users': [_owner('bob', 'Bob Builder')]}
    good = _crate('fastcsv', 'https://www.fastcsv.dev/docs',
                  'https://github.com/acme/fastcsv')
    row, = _parse_owners(good, owner_response)
    assert row.website == 'https://www.fastcsv.dev/docs'
    assert row.domain == 'fastcsv.dev'

    github_homepage = _crate('fastcsv', 'https://github.com/acme/fastcsv')
    row, = _parse_owners(github_homepage, owner_response)
    assert row.website == 'https://github.com/acme/fastcsv'
    assert row.domain == ''

    custom_repo = _crate('fastcsv', '', 'https://forge.fastcsv.dev/repo')
    row, = _parse_owners(custom_repo, owner_response)
    assert row.website == 'https://forge.fastcsv.dev/repo'




def test_shared_get_json_signature_is_compatible_and_single_attempt(monkeypatch):
    """Use the real shared JSON helper, replacing only its network boundary.

    This catches unsupported keywords missed by mocked get_json(**kwargs).
    The production _http.get_json has no retries parameter and attempts
    exactly once, so crates.io remains within the source's rate limit.
    """
    import urllib.error
    from openleads import _http
    import openleads.sources.crates as crates

    calls = []

    def fake_open(url, headers=None, timeout=15, data=None):
        calls.append((url, headers))
        if url.endswith("q=ok"):
            return '{"crates":[]}'
        raise urllib.error.URLError("offline fixture failure")

    monkeypatch.setattr(_http, "_open", fake_open)
    monkeypatch.setattr(crates, "get_json", _http.get_json)
    monkeypatch.setattr(crates, "_clock", lambda: 0.0)
    monkeypatch.setattr(crates, "_sleep", lambda seconds: None)
    monkeypatch.setattr(crates, "_next_start", 0.0)

    ok = crates._get("https://crates.io/api/v1/crates?q=ok", cache=None)
    fail = crates._get("https://crates.io/api/v1/crates?q=error", cache=None)
    assert ok == {"crates": []}
    assert fail is None
    assert len(calls) == 2  # one attempt per distinct request; no implicit retry
    assert all(c[1]["User-Agent"].startswith("OpenLeads-crates/") for c in calls)


def test_http_helper_retry_contract_legacy_and_modern(monkeypatch):
    """The modern shared helper retries unless told not to; crates must not."""
    import openleads.sources.crates as crates

    clock = [100.0]
    monkeypatch.setattr(crates, '_clock', lambda: clock[0])
    monkeypatch.setattr(crates, '_sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(crates, '_next_start', 0.0)
    calls = []

    def legacy_json(url, headers=None, cache=None, ttl_ns=None):
        calls.append(('legacy', url, ttl_ns))
        return {'ok': True}

    monkeypatch.setattr(crates, 'get_json', legacy_json)
    assert crates._get('https://crates.io/api/v1/crates', None) == {'ok': True}

    def modern_json(url, headers=None, cache=None, ttl_ns=None, retries=None):
        calls.append(('modern', url, retries))
        return {'ok': True}

    monkeypatch.setattr(crates, 'get_json', modern_json)
    assert crates._get('https://crates.io/api/v1/crates', None) == {'ok': True}
    assert calls[0][0] == 'legacy'
    assert calls[1] == ('modern', 'https://crates.io/api/v1/crates', 0)
    assert clock[0] >= 101.05  # the cross-call 1/sec guard still applies
