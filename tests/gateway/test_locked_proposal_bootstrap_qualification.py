"""Qualification only: a local operator ticket authenticates an OS peer, not human presence.

This is intentionally not an approval or enrollment test. No live endpoint is contacted.
"""
import asyncio
import json
import os

import pytest


@pytest.mark.platforms('linux')
@pytest.mark.asyncio
async def test_same_native_peer_can_select_worker_or_interactive_purpose(tmp_path):
    from gateway.control_socket import GatewayControlServer, resolve_client_socket_path
    from gateway.runtime_bootstrap import TicketStore
    home = tmp_path / 'owned'
    home.mkdir(mode=0o700)
    profile = str(home.resolve())
    server = GatewayControlServer(home)
    server.ticket_store = TicketStore('qualification-instance', frozenset({profile}))
    assert await server.start()

    async def grant(purpose):
        reader, writer = await asyncio.open_unix_connection(str(resolve_client_socket_path(home)))
        try:
            writer.write(json.dumps({'protocol': 1, 'verb': 'session-ticket', 'params': {
                'profile_id': profile, 'instance_id': 'qualification-instance', 'purpose': purpose}}).encode() + b'\n')
            await writer.drain()
            reply = json.loads(await asyncio.wait_for(reader.readline(), 5))
            assert reply['ok']
            return server.ticket_store.redeem(reply['result']['ticket'], profile_id=profile, purpose=purpose)
        finally:
            writer.close()
            await writer.wait_closed()

    try:
        worker = await grant('worker-adoption')
        interactive = await grant('interactive')
        assert worker['subject'] == interactive['subject'] == f'uid:{os.getuid()}'
        assert worker['capabilities'] == frozenset({'worker:adopt'})
        assert 'session:approve' in interactive['capabilities']
        assert interactive['purpose'] == 'interactive'
    finally:
        await server.stop()
