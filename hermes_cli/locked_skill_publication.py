"""Read-only qualification of the one supported publication route: same-repo GitHub PR."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

from hermes_cli.locked_skill_proposals import ProposalError, git


def _read_pull(repository, number, *, home=None):
    from hermes_constants import get_hermes_home
    from tools.environments.local import _is_routed_home, served_profile_child_env
    home = Path(home) if home is not None else get_hermes_home()
    env = served_profile_child_env(target_home=home, inherit_credentials=True)
    if _is_routed_home(home):
        # gh's OAuth directory is also authority: never fall back to the launch
        # account when the served owner has no credential of its own.
        if not (env.get('GH_TOKEN') or env.get('GITHUB_TOKEN')):
            raise ProposalError('owning-profile GitHub token required for routed PR reads')
        env['GH_CONFIG_DIR'] = str(home / '.config/gh')
    else:
        # The launch profile owns these shell/systemd credentials; the generic
        # sanitizer omits them even when no cross-profile boundary is involved.
        env.update({key: os.environ[key] for key in ('GH_TOKEN', 'GITHUB_TOKEN') if key in os.environ})
    result = subprocess.run(['gh', 'api', '--hostname', 'github.com', '--method', 'GET', f'repos/{repository}/pulls/{number}'],
                            env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=False)
    if result.returncode:
        raise ProposalError('GitHub PR read failed')
    return json.loads(result.stdout)


def _repository(remote):
    matched = re.fullmatch(r'(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?', remote)
    if matched is None:
        raise ProposalError('only an owning GitHub repository is supported')
    return matched[1]


def bind(manifest, pr_url):
    remote = git(manifest, 'remote', 'get-url', 'origin').decode().strip()
    repository = _repository(remote)
    match = re.fullmatch(r'https://github\.com/' + re.escape(repository) + r'/pull/([1-9][0-9]*)', pr_url or '')
    if match is None:
        raise ProposalError('exact owning-repository PR URL required')
    # Managed repositories must agree with the operator ownership manifest as well.
    if '/' in manifest['repository'] and not manifest['repository'].startswith('/') and manifest['repository'] != repository:
        raise ProposalError('PR repository differs from registered owner')
    value = {**manifest, 'pr_url': pr_url, 'github_repository': repository, 'pr_number': int(match[1]),
             'origin': remote}
    verify(value)
    return value


def verify(manifest):
    remote = git(manifest, 'remote', 'get-url', 'origin').decode().strip()
    if remote != manifest['origin'] or _repository(remote) != manifest['github_repository']:
        raise ProposalError('owning repository origin changed')
    pull = _read_pull(manifest['github_repository'], manifest['pr_number'], home=manifest['home'])
    try:
        valid = (pull['html_url'] == manifest['pr_url'] and pull['state'] == 'open' and not pull['merged']
                 and pull['head']['repo']['full_name'] == pull['base']['repo']['full_name'] == manifest['github_repository']
                 and pull['head']['sha'] == manifest['head'] and pull['base']['sha'] == manifest['base']
                 and pull['head']['ref'] == manifest['branch'] and pull['base']['ref'] == 'main')
    except (KeyError, TypeError) as exc:
        raise ProposalError('unreadable PR identity') from exc
    if not valid:
        raise ProposalError('PR revision/base/route changed; reconcile and refreeze')
