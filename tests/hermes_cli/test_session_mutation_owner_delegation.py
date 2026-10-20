"""A standalone dashboard hands session edits to the live owner instead of 409ing.

A standalone ``hermes dashboard``/``serve`` composes no authority, so its mutation path is the
offline one — which reserves the exclusive owner lock *through the write*. While a gateway runs
that lock is already held, so every sidebar edit died with ``409 Exclusive maintenance refused``
(and a copy telling the Desktop client to "drain and stop the gateway", which it cannot do).

The owner is the only legal writer, so the mutation is replayed at it over native HTTP.
"""
import httpx
import pytest
from fastapi import FastAPI

from gateway.runtime_ownership import ProfileOwnership
from hermes_cli.web_routers.sessions import manage_router
from hermes_state import SessionDB

AUTH = {'Authorization': 'Bearer standalone-token'}


def _standalone_app(tmp_path, monkeypatch):
    from hermes_cli import web_server
    monkeypatch.setattr(web_server, '_SESSION_TOKEN', 'standalone-token')
    monkeypatch.setattr('hermes_state._default_db_path', lambda: tmp_path / 'state.db')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    app = FastAPI()
    app.include_router(manage_router)
    return app


@pytest.mark.asyncio
async def test_edit_is_forwarded_to_the_live_owner_not_refused(tmp_path, monkeypatch):
    """A discoverable ready owner takes the mutation; the offline lock is never contended."""
    from hermes_cli import web_server_sessions as wss

    app = _standalone_app(tmp_path, monkeypatch)
    endpoint = type('Endpoint', (), {'api_origin': 'http://127.0.0.1:1',
                                     'profile_id': str(tmp_path)})()
    seen = {}

    monkeypatch.setattr(wss, '_live_owner', lambda home: endpoint)

    async def fake_forward(request, owner, home):
        seen['owner'] = owner
        seen['path'] = request.url.path
        seen['method'] = request.method
        return {'ok': True, 'revision': 7, 'forwarded': True}

    monkeypatch.setattr(wss, '_forward_mutation_to_owner', fake_forward)

    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('owned', source='cli')
        owner = ProfileOwnership()
        owner.reserve([tmp_path])  # a live gateway holds the store
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url='http://localhost', headers=AUTH) as client:
                edited = await client.patch('/api/sessions/owned',
                                            json={'request_id': 'r1', 'expected_revision': 0,
                                                  'title': 'X'})
                assert edited.status_code == 200, edited.text
                assert edited.json()['revision'] == 7
                assert seen == {'owner': endpoint, 'path': '/api/sessions/owned',
                                'method': 'PATCH'}
        finally:
            owner.close()
        # The standalone process wrote nothing: the owner is the only writer.
        assert db.get_session('owned')['title'] is None


@pytest.mark.asyncio
async def test_no_owner_still_reserves_the_offline_lock(tmp_path, monkeypatch):
    """Discovery finding nothing keeps the old offline refusal: no silent second writer."""
    from hermes_cli import web_server_sessions as wss

    app = _standalone_app(tmp_path, monkeypatch)
    monkeypatch.setattr(wss, '_live_owner', lambda home: None)

    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('owned', source='cli')
        owner = ProfileOwnership()
        owner.reserve([tmp_path])
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url='http://localhost', headers=AUTH) as client:
                refused = await client.patch('/api/sessions/owned',
                                             json={'request_id': 'r1', 'expected_revision': 0,
                                                   'title': 'X'})
                assert refused.status_code == 409, refused.text
        finally:
            owner.close()
        assert db.get_session('owned')['title'] is None


@pytest.mark.asyncio
async def test_forward_replays_query_and_body_at_the_owner(tmp_path, monkeypatch):
    """The replay preserves method, path, query and body — no per-operation payload rebuild."""
    from hermes_cli import web_server_sessions as wss

    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {'ok': True, 'deleted_ids': ['x']}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, content=None, headers=None):
            captured.update(method=method, url=url, content=content, headers=headers)
            return FakeResponse()

    monkeypatch.setattr(httpx, 'AsyncClient', FakeClient)
    monkeypatch.setattr('hermes_cli.gateway_client._session_ticket',
                        lambda home, endpoint, purpose=None: 'grant-test')

    request = type('Req', (), {
        'method': 'DELETE',
        'url': type('URL', (), {'path': '/api/sessions/x',
                                'query': 'request_id=r1&expected_revision=2'})(),
        'headers': {'content-type': 'application/json'},
        'body': staticmethod(lambda: _async(b'{"expected_generation":0}')),
    })()
    endpoint = type('Endpoint', (), {'api_origin': 'http://127.0.0.1:9/',
                                     'profile_id': 'p'})()

    result = await wss._forward_mutation_to_owner(request, endpoint, tmp_path)
    assert result == {'ok': True, 'deleted_ids': ['x']}
    assert captured['method'] == 'DELETE'
    assert captured['url'] == 'http://127.0.0.1:9/api/sessions/x?request_id=r1&expected_revision=2'
    assert captured['content'] == b'{"expected_generation":0}'
    assert captured['headers']['X-Hermes-Gateway-Ticket'] == 'grant-test'


async def _async(value):
    return value
