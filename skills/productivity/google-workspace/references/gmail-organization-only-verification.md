# Gmail organization-only verification

Use this when the Gmail workflow should be limited to organization tasks only.

## Goal

Allow:
- read full emails
- list/create/update/delete labels
- apply/remove labels on messages

Block:
- send
- reply
- trash/delete messages

## What bit us

The repo copy under `~/.hermes/hermes-agent/skills/...` had the updated organization-only Gmail wrapper, but the live installed skill under `~/.hermes/skills/...` was still stale and still exposed `send`/`reply`. Hermes loads the installed skill tree at runtime, so verify the live copy, not just the repo copy.

## Verification

Check both copies if Brian has a local Hermes checkout:

```bash
python3 ~/.hermes/hermes-agent/skills/productivity/google-workspace/scripts/google_api.py gmail --help
python3 ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py gmail --help
```

Expected live subcommands:

```text
{search,get,labels,modify}
```

Verify blocked actions are not exposed:

```bash
python3 ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py gmail send --to test@example.com --subject Hi --body Hello
python3 ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py gmail reply 123 --body Hello
```

Expected result: argparse rejects `send` and `reply` as invalid choices.

Verify label management is exposed:

```bash
python3 ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py gmail labels --help
```

Expected label subcommands:

```text
{list,create,update,delete}
```

Search the wrapper for forbidden message-destruction paths:

```bash
grep -nE 'trash|messages\.delete|messages\.trash' ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py
```

Expected result: no matches.

## If repo and live copies differ

Sync the live installed skill from the repo copy, then re-run the verification commands:

```bash
cp ~/.hermes/hermes-agent/skills/productivity/google-workspace/scripts/google_api.py ~/.hermes/skills/productivity/google-workspace/scripts/google_api.py
cp ~/.hermes/hermes-agent/skills/productivity/google-workspace/SKILL.md ~/.hermes/skills/productivity/google-workspace/SKILL.md
```

## Scope nuance

`gmail labels delete LABEL_ID` deletes labels, not messages. That still fits an organization-focused workflow unless Brian says label deletion should be blocked too.
