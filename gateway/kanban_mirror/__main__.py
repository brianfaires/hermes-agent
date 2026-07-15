"""CLI: python -m gateway.kanban_mirror --dry-run | --once | --rebuild [--adopt-legacy] [--live]"""
import argparse
import asyncio

from gateway.kanban_mirror.config import load_mirror_config
from gateway.kanban_mirror.daemon import rebuild, tick
from gateway.kanban_mirror.discord_client import DiscordClient, load_discord_token
from gateway.kanban_mirror.state import connect_mirror, mirror_db_path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run one live tick")
    ap.add_argument("--dry-run", action="store_true", help="print the op plan, write nothing")
    ap.add_argument("--rebuild", action="store_true", help="bootstrap initiatives from scratch")
    ap.add_argument("--adopt-legacy", action="store_true", help="with --rebuild: adopt/archive v1 threads")
    ap.add_argument("--live", action="store_true", help="with --rebuild: actually write (default prints plan only)")
    ap.add_argument("--board", default=None)
    args = ap.parse_args()
    cfg = load_mirror_config()
    if args.board:
        from dataclasses import replace
        cfg = replace(cfg, board=args.board)
    dry = args.dry_run or (args.rebuild and not args.live)
    client = None
    if not dry:
        token = load_discord_token(cfg.token_env_path)
        if not token:
            raise SystemExit(f"no DISCORD_BOT_TOKEN at {cfg.token_env_path}")
        client = DiscordClient(token)
    conn = connect_mirror(mirror_db_path(cfg.board))
    fn = rebuild(cfg, client, conn, dry_run=dry, adopt_legacy=args.adopt_legacy) if args.rebuild \
        else tick(cfg, client, conn, dry_run=dry, allow_llm=not args.dry_run)
    for line in asyncio.run(fn):
        print(line)


if __name__ == "__main__":
    main()
