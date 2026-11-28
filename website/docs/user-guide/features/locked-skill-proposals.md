---
title: Locked-skill proposals
description: Exact-revision human approval without unlocking a skill
---

# Locked-skill proposals

A lock still refuses ordinary agent, curator, subagent, file, patch and terminal
writes. A proposal is **not an unlock**. `skills.write_approval` remains false by
default; the proposal's native human subcommand does not change that setting.
This narrow workflow handles one existing tracked regular `SKILL.md` in one
profile-owned repository. Supporting scripts, new files, renames, deletions,
symlinks, submodules and mode changes are not supported.

## Prerequisites and trust envelope

The source capability must first pass review, CI and the normal gateway release
process. Source preparation is not activation. The first version requires a POSIX
native owner with real file locking, an owning GitHub `origin`, and an **open,
same-repository PR targeting main**, containing one commit directly on the
unchanged installed base. The existing `gh` authentication must be available to
the owner for read-only PR verification. Git subprocesses carry no launch-profile
credentials. A routed owner must have its own `GH_TOKEN` or `GITHUB_TOKEN` in its
existing native secret scope; a miss refuses the PR read, never borrowing the
launch profile's token or OAuth directory. Same-process owners may reuse their
existing native gh authentication. These are existing credential inputs, not a
credential-provisioning endpoint; no credential is accepted in proposal payloads.

A ready authenticated native owner must already be running. These CLI commands do
not start/restart a gateway, fall back to direct database writes, publish a PR,
merge a PR or push a branch.

A local OS operator is trusted to run an **attended provisioning ceremony**, but
is not inferred to be Brian. After reviewed release, the authorized provisioner:

1. Runs `hermes skills proposal enroll-start CATEGORY/SKILL` for the approved
   owner/skill scope. The capture challenge expires after 15 minutes.
2. Brian sends `/skills proposal register IDENTIFIER` in the intended native
   Telegram private conversation. Forwarded, edited, bot, relay and routed-to-a-
   different-home events are refused. The native response gives a candidate
   fingerprint, not an identity inferred from environment/model text.
3. Brian confirms that exact capture from the same authenticated account with
   `/skills proposal confirm IDENTIFIER CANDIDATE_DIGEST`.
4. The authorized provisioner **independently verifies Brian's confirmation and
   exact capture** before running
   `hermes skills proposal enroll-finish IDENTIFIER CANDIDATE_DIGEST --attended-confirmation`.
   The flag records the provisioner's assertion; it is not proof of human presence.

This registration is one-time, owner/home/repository/skill/path scoped. It grants
no generic admin rights and approves no proposed content. State is stored by the
native owner in its protected `state.db` (`state_meta`, prefix
`skills.locked_proposals.v1:`, principal key suffix `principal`). There is no
editable JSON approval record and no model tool that writes this grant. This is
not a hostile-same-UID sandbox or a new identity/approval platform.

## Prepare, publish, freeze

1. As the owning profile, use
   `hermes skills proposal prepare CATEGORY/SKILL /trusted/new/worktree`.
   It checks the existing lock and committed live preimage, then creates an
   isolated worktree. It returns an identifier, branch and worktree. Live skill,
   lock bytes, HEAD and index are unchanged. Only the isolated proposal may be
   edited; this is separate from installation.
2. Edit that one existing `SKILL.md` and make one commit in the proposal worktree.
   Through the existing owner publication policy, publish the proposal branch and
   create the actual PR. Publication requires its own authorization. Do not use
   the legacy accept helper or a broad repository sync to install the skill.
3. Run `hermes skills proposal freeze IDENTIFIER PR_URL`.
   The whole unfiltered revision must modify only that file. Freeze records the
   repository/common directory, native profile ID/canonical home, skill/path,
   complete base/head/blob identities, modes, exact raw pre/postimages, full
   binary diff and owning PR identity in a protected immutable manifest. Its
   SHA-256 is the approval identity. Inspect it with
   `hermes skills proposal show MANIFEST_DIGEST`.

The publisher brings the actual PR, summary, full head SHA and manifest digest
to the registered Telegram conversation. No generic “SGTM,” bot quote, PR label,
review, `Authorized-by` trailer, local CLI call or unattended job is approval.

## Exact approval and separate apply

Brian approves only the displayed frozen manifest with:

```text
/skills proposal approve MANIFEST_DIGEST
```

The gateway verifies the authenticated non-bot SDK sender, native callback,
transport/runtime home, current connector, registered principal, exact command,
manifest and current PR/base/content. It stores an exact approval receipt. It
does **not** apply, merge, publish or reload anything.

The native owner separately performs:

```text
hermes skills proposal apply MANIFEST_DIGEST
```

Apply rechecks the receipt, target/revision/PR, readable lock, preimage, file
identity, safe path and mode. Under existing skill-mutation and usage locks it
atomically replaces only that file with the approved bytes, fsyncs, verifies
exact readback and records an application receipt. Unrelated content and the
lock are unchanged. If interrupted between replacement and receipt, the durable
intent and exact destination inode/postimage identify the write it owns. An
identical retry recovers/reports already-applied; changed bytes or a replacement
inode stop it instead of blindly repeating the write.

The target's locked/pinned protection semantics are bound strictly; ordinary usage
counters (including other skills' views) are not approval identity. Every current
lock read still rejects missing, unreadable, duplicate, malformed or unlocked data.
Unrelated telemetry cannot strand an exact content-write recovery.

Content, target, base, head, mode, connector or PR changes invalidate the affected
authorization. Reconcile the proposal, refreeze and obtain new exact approval.
Unreadable/missing locks or malformed/missing receipts fail closed. Do not
substitute another writer or relax settings after a refusal.

## Git reconciliation, remote publication and use

`hermes skills proposal reconcile MANIFEST_DIGEST` is a **separate local metadata
step after successful apply**. It advances only the owner's main ref to the exact
already-approved commit and updates only that file's index entry; no checkout,
merge of live files, new commit, broad add, cron sync, push or automatic PR merge.
Dirty and staged unrelated content stays unchanged. The durable reconciliation
record permits recovery only through the exact base/approved-head index states;
concurrent head/index/target drift refuses the operation. Git's `index.lock` is
held from revalidation through native temporary-index replacement, ref compare-
and-swap and the completion receipt. Unrelated index entries are verified
unchanged. Pending recovery is fenced by exact before/after index hashes; completed
replay requires the approved entry, never a staged reverse change. An existing or
stale Git lock is a refusal, not something this feature deletes or bypasses.
Process-crash lock recovery retains the existing owner's Git safety procedure;
there is no automatic stale-lock cleanup or alternate executor.

Remote acceptance/publication is still the owner's existing scoped workflow.
Inspect the entire outgoing ancestry and diff, not just its last commit. The
approved branch is already one single-file commit on the unchanged base; do not
push unrelated outgoing commits or force-push. A server merge result that changes
the frozen identity requires reconciliation/refreeze/reapproval, not an inherited
approval. Generic sync is not a safe substitute on a dirty profile repository.

Saved bytes, local Git reconciliation, remote PR/merge/publication, and runtime
use are separate states. Existing conversations may retain the old prompt
snapshot. Verify a fresh native skill load before claiming runtime acceptance;
editing prose is not a reason for an arbitrary gateway restart.
