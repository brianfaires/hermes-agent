#!/usr/bin/env python3
"""No-agent scheduler entrypoint; success/empty is silent, failures sanitized."""
import argparse
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', required=True)
    parser.add_argument('--reconcile-entry')
    parser.add_argument('--confirmed-remote-outcome', choices=['present', 'absent'])
    parser.add_argument('--cleanup', action='store_true')
    parser.add_argument('--delete-holding-duplicates', action='store_true')
    args = parser.parse_args()
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(args.home)
    from agent.secret_scope import build_profile_secret_scope, set_secret_scope, reset_secret_scope
    scope = set_secret_scope(build_profile_secret_scope(Path(args.home)))
    from plugins.private_journal import storage
    marker = Path(args.home) / 'journal' / 'failure-notified'
    try:
        from plugins.private_journal.runtime import run_batch, settings
        if args.reconcile_entry:
            from plugins.private_journal.memory import reconcile
            reconcile(args.reconcile_entry, bank=settings()['bank_id'], outcome=args.confirmed_remote_outcome)
        elif args.cleanup:
            from plugins.private_journal.retention import cleanup
            cleanup(vault_path=settings()['vault_path'], delete_holding_duplicates=args.delete_holding_duplicates)
        elif args.delete_holding_duplicates or args.confirmed_remote_outcome:
            raise ValueError('invalid flags')
        else:
            run_batch()
        if marker.exists():
            storage.remove_verified(marker, b'failed\n')
        return 0
    except Exception:
        try:
            if not marker.exists():
                storage.publish(marker, b'failed\n', verify=False)
                print('Private journal batch failed; data retained.\nCheck configuration or reconcile pending memory intent.', file=sys.stderr)
        except Exception:
            print('Private journal batch failed; private status unavailable.', file=sys.stderr)
        return 1
    finally:
        reset_secret_scope(scope)
        reset_hermes_home_override(token)


if __name__ == '__main__':
    try:
        status = main()
    except Exception:
        print('Private journal batch failed; configuration unavailable.', file=sys.stderr)
        status = 1
    raise SystemExit(status)
