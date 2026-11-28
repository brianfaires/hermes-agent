"""Explicit proposal-only CLI; all protected state stays in the existing native owner."""
from __future__ import annotations

import asyncio
import json


def build_parser(subparsers):
    parser = subparsers.add_parser('proposal', help='Propose one existing locked SKILL.md; never unlock it')
    actions = parser.add_subparsers(dest='proposal_action', required=True)
    prepare = actions.add_parser('prepare', help='Create an isolated committed-source worktree only')
    prepare.add_argument('skill', help='Exact skills-relative package path')
    prepare.add_argument('destination', help='New absolute worktree path outside all live roots')
    freeze = actions.add_parser('freeze', help='Freeze one clean single-file commit and its owning PR')
    freeze.add_argument('identifier')
    freeze.add_argument('pr_url')
    for action in ('show', 'apply', 'reconcile'):
        operation = actions.add_parser(action)
        operation.add_argument('digest', help='Exact frozen manifest SHA-256')
    start = actions.add_parser('enroll-start', help='Start an attended native principal registration ceremony')
    start.add_argument('skill')
    finish = actions.add_parser('enroll-finish', help='Finish only after independent attended confirmation')
    finish.add_argument('identifier')
    finish.add_argument('digest', help='Exact captured identity fingerprint confirmed in native Telegram')
    finish.add_argument('--attended-confirmation', action='store_true', required=True,
                        help='Trusted provisioner has independently verified Brian confirmed this exact capture')


async def _request(params):
    from hermes_constants import get_hermes_home
    from hermes_cli.gateway_client import GatewayClientError, connect_gateway, outside_restart_wait
    from hermes_cli.gateway_runtime import discover_gateway_endpoint
    discovered = await asyncio.to_thread(discover_gateway_endpoint, get_hermes_home())
    if discovered.state != 'ready' or discovered.endpoint is None:
        raise GatewayClientError('A ready native owner is required; no start or local-database fallback')
    with outside_restart_wait():
        async with connect_gateway(discovered.endpoint) as client:
            return await client.rpc('skills.proposal', _timeout=120, **params)


def command(args):
    from gateway.locked_skill_endpoints import _FIELDS
    from hermes_cli.gateway_client import GatewayClientError
    operation = args.proposal_action
    params = {'operation': operation, **{key: getattr(args, key) for key in _FIELDS[operation]}}
    try:
        print(json.dumps(asyncio.run(_request(params))['result'], indent=2, sort_keys=True))
        return 0
    except (GatewayClientError, TimeoutError) as exc:
        print('Locked proposal refused: ' + str(exc))
        return 1
