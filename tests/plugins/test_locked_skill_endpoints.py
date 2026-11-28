"""Owner endpoint never accepts an approval supplied by a local client."""
from types import SimpleNamespace

import pytest

pytest_plugins = ['tests.plugins.test_locked_skill_boundaries']
pytestmark = pytest.mark.platforms('posix')


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [
    {'operation': ['approve'], 'digest': '0' * 64},
    {'operation': 'approve', 'digest': '0' * 64},
    {'operation': 'apply', 'digest': '0' * 64, 'approval': {'approved': True}},
    {'operation': 'enroll-finish', 'identifier': 'x', 'digest': '0' * 64},
])
async def test_local_payloads_cannot_mint_or_supply_approval(workflow, payload):
    from gateway.locked_skill_endpoints import rpc
    from hermes_state_runtime import RuntimeStoreError
    case = workflow
    connection = SimpleNamespace(authority=case.service.authority, native_owner=True,
        actor=SimpleNamespace(profile_id=str(case.home), capabilities={'session:operator'}))
    with pytest.raises(RuntimeStoreError):
        await rpc(connection, None, payload)


@pytest.mark.asyncio
@pytest.mark.parametrize('native,capability', [(False, True), (True, False)])
async def test_owner_endpoint_refuses_untrusted_transport(workflow, native, capability):
    from gateway.locked_skill_endpoints import rpc
    from hermes_state_runtime import RuntimeStoreError
    case = workflow
    connection = SimpleNamespace(authority=case.service.authority, native_owner=native,
        actor=SimpleNamespace(profile_id=str(case.home), capabilities={'session:operator'} if capability else set()))
    with pytest.raises(RuntimeStoreError):
        await rpc(connection, None, {'operation': 'apply', 'digest': case.fingerprint})


@pytest.mark.asyncio
async def test_native_gateway_approval_works_with_write_staging_disabled(workflow):
    from gateway.session_ingress_context import native_callback
    from gateway.slash_commands import GatewaySlashCommandsMixin
    from gateway.locked_skill_endpoints import rpc
    case = workflow
    args = ['approve', case.fingerprint]
    received = case.event(args)
    before = case.skill.read_bytes()
    with native_callback(case.runner, received, case.home):
        output = await GatewaySlashCommandsMixin._handle_skills_command(case.runner, received)
    assert 'approved' in output
    assert case.skill.read_bytes() == before
    connection = SimpleNamespace(authority=case.service.authority, native_owner=True,
        actor=SimpleNamespace(profile_id=str(case.home), capabilities={'session:operator'}))
    result = await rpc(connection, None, {'operation': 'apply', 'digest': case.fingerprint})
    assert result['result']['applied']
    assert case.skill.read_bytes() == b'Reviewed exact bytes\n'


@pytest.mark.asyncio
async def test_canonical_owner_wire_has_no_approve_operation(workflow):
    from gateway.session_authority import SessionAuthority
    from gateway.session_controls import AuthorityConnection
    case = workflow
    case.runner._draining = False
    authority = SessionAuthority(case.runner, profile_id=str(case.home), instance_id='synthetic',
                                 db=case.service.db, epoch=1)
    connection = AuthorityConnection(authority, SimpleNamespace(), {
        'user_id': 'uid:synthetic', 'profile_id': str(case.home), 'instance_id': 'synthetic',
        'native_bootstrap': True}, operator=True)
    result = await connection.dispatch({'id': 1, 'method': 'skills.proposal',
                                       'params': {'operation': 'show', 'digest': case.fingerprint}})
    assert result['result']['result']['manifest_sha256'] == case.fingerprint
    denied = await connection.dispatch({'id': 2, 'method': 'skills.proposal',
                                       'params': {'operation': 'approve', 'digest': case.fingerprint}})
    assert denied['error']['data']['reason'] == 'invalid_params'
