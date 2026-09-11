# Approved restoration batch 3: locally qualified source

All 26 pending approved IDs plus G32 are implemented and independently reviewed. No remaining scoped restoration ID is pending. Production/test candidate `31bf95b85160ec4b205b28b01f5f4b99a2caa802`, tree `c3805ee8ae23c20ebdcaa3a25426b6f4d4e2eaa0`, continues Ang-accepted `75a30e9809eb2db9edd40fb741c6a43d717c8fb8` on the same source branch. The final documentation commit/tree is bound in the external approved-batch3-evidence.md receipt to avoid a self-referential commit claim.

Critical classifications remain. Voice, retired policy/consumers and archive-only features remain excluded. This is local qualification of the approved outcomes, not a whole-product parity or full CI claim. First-batch hosted CI 34545497788 FAILED for 4cef65cada (one fixture case plus aggregate); correction 75a30e98 was independently accepted by Ang. Ang reported fresh CI starting for that preceding candidate; no completed green result is asserted. Ang must integrate/verify this exact candidate and require its hosted CI.

Ang staging integration and push are authorized. Source workers do not push or mutate staging/main/canonical/live state. No installation, live config/env/data changes, paid services, gateway restart, delivery, real process-killer execution or voice work was performed. Focused tests use canonical read-only Python and per-file sanitized task-local homes. Packaging uses actual pip/uv no-install resolution only.

## Exact scope and compatibility decisions

- **FC-10-E02**: Profile+route rate keys and profile+route+delivery idempotency/cache/session keys; raw external IDs retained, legacy toolset source reads preserved. Owning commits: `f5c98229e3784250604f35e8cdc030ab82134aa7`.
- **FC-11-E08**: Shared strict plain-string name and list-of-identifier toolset normalization at create/update; rejected writes preserve stored bytes and legacy reads remain tolerant. Owning commits: `f5c98229e3784250604f35e8cdc030ab82134aa7`.
- **FC-14-E01**: Literal prose escaping after table conversion with code spans preserved. Owning commits: `ad7e4dc28466fd134b27c86f256dfca26605f1fc`.
- **FC-14-E02**: Only standalone MEDIA directives extract/clean; original-line protected-span eligibility and document-marker cleanup agree; existing file validation and denial retained. Owning commits: `ad7e4dc28466fd134b27c86f256dfca26605f1fc`, `97ad45b32cbd9df946662860192aaaa446394502`, `8741695f9ad9d73b770ed7e2969579d5be8e5e74`, `b411e475e24abd72eaae3a8810f8004a21f4660e`.
- **FC-15-E01**: Configured auto-thread includes free-response channels, explicit no-thread and other guards remain; failure never falls through to agent inline. Owning commits: `ad7e4dc28466fd134b27c86f256dfca26605f1fc`.
- **FC-17-E17**: Launch-home-owned runtime publication with scoped restoration; existing adapter maps govern connected membership without resurrecting retired Kanban runtime mixin. Owning commits: `4775bf2b0396d8a224eface290744213c9ff4608`.
- **FC-17-E18**: CLI coverage requires served+connected membership and existing PID/start-time/home validation; stale/malformed/missing records fail closed. Owning commits: `4775bf2b0396d8a224eface290744213c9ff4608`.
- **FC-17-E29**: Successful connected_profiles membership updated on startup/reconnect/fatal/shutdown; configured served_profiles remains routing/cron eligibility for current adapterless consumers. Owning commits: `4775bf2b0396d8a224eface290744213c9ff4608`.
- **FC-18-E03**: Persistent dir/worktree branches supported; scratch rejects, inherited board workdir remains available. Owning commits: `7bc38d6fe36f1ca079f6c41e319f816f5b7f87ac`, `105608061f34bd14614500143c11baba99e7fcb1`.
- **FC-18-E04**: Dashboard branch_name model and forwarding share canonical DB validation. Owning commits: `7bc38d6fe36f1ca079f6c41e319f816f5b7f87ac`, `105608061f34bd14614500143c11baba99e7fcb1`.
- **FC-18-E05**: Validate complete final create/set metadata inside transaction after derivation; legacy invalid paths reject branch mutation atomically. Owning commits: `7bc38d6fe36f1ca079f6c41e319f816f5b7f87ac`, `e39c7aaefa38c2d37b730076f2da30e06ec23569`, `105608061f34bd14614500143c11baba99e7fcb1`.
- **FC-20-E12**: Copy and remove every HERMES_KANBAN_* child key while retaining delegated lineage marker and durable mutation refusal; no writable scratch-board escape hatch. Owning commits: `f5c98229e3784250604f35e8cdc030ab82134aa7`.
- **FC-25-E04**: Complete preserved tail joins before core atomic publication; gateway adopts canonical child without destructive post-publication rewrite or unsafe child rollback. Original partial boundary survives durable-parent adoption; todo/user transforms apply to head only, explicit-tail growth refuses without salvage. Owning commits: `71da39be8ece2dc9f8478647bee748c8563e394e`, `31bf95b85160ec4b205b28b01f5f4b99a2caa802`.
- **FC-26-default**: Approved default description budget1024; explicit caller budgets preserved, new prompt builds only. Owning commits: `49b056b85f41472cb21de9ebb54270252110fff7`.
- **FC-27-E01**: Bounded guidance for exact sol/terra model IDs with optional one provider prefix; common mandatory-tool guidance and tool filtering retained. Owning commits: `49b056b85f41472cb21de9ebb54270252110fff7`.
- **FC-27-E02**: Variant selection follows existing independent execution-guidance gates; repeated prompt builds byte-stable. Owning commits: `49b056b85f41472cb21de9ebb54270252110fff7`.
- **FC-28-E02**: Explicit discord.py/PyNaCl/davey requirements agree across pip and uv surfaces; actual no-install resolution succeeds. Davey0.1.4->0.1.6 is intentional historical pin; other third-party versions/hashes unchanged. Owning commits: `3354b0a08e110a9ef067cc8beaba5aeee848e676`.
- **FC-30-E01**: Pure wrapper/executable-aware process classifier preserves subtree fences, full-python pattern blocks and harmless-argument distinction; no killer commands executed. Owning commits: `a87ac195800888b334a4b977d19b1419c44d99b2`, `eef1c3a41dbdd9fc2870c4580e39e3a6181f4fa3`, `6f78aabd57afc2ffd551e2855b0e9e5e81890558`.
- **FC-30-E03**: Pinned reusable lockfile scan unconditional; code-scanning publication gated to exact upstream repository only. Owning commits: `a87ac195800888b334a4b977d19b1419c44d99b2`.
- **FC-30-E04**: Explicit upload-sarif input verified against exact pinned official workflow; artifact result path and scan retained everywhere. Owning commits: `a87ac195800888b334a4b977d19b1419c44d99b2`.
- **FC-33-E01**: Evaluate all findings before display cap; exact case-sensitive package pair warning suppression only, BLOCK verdict/evidence and malformed warning refusal retained. Owning commits: `a87ac195800888b334a4b977d19b1419c44d99b2`.
- **FC-19-E01**: Shared profile-owned notification policy enforced consistently at subscription and delivery; malformed/missing foreign configuration denies; redirect metadata sanitized and original row/claim/cursor preserved. CLI/tool/slash/dashboard paths qualified; TUI subscription compatibility retained. Owning commits: `674378e8597a78df67f4ff7a3dcb937262286289`, `105608061f34bd14614500143c11baba99e7fcb1`.
- **FC-19-E02**: Shared profile-owned notification policy enforced consistently at subscription and delivery; malformed/missing foreign configuration denies; redirect metadata sanitized and original row/claim/cursor preserved. CLI/tool/slash/dashboard paths qualified; TUI subscription compatibility retained. Owning commits: `674378e8597a78df67f4ff7a3dcb937262286289`, `105608061f34bd14614500143c11baba99e7fcb1`.
- **FC-19-E03**: Shared profile-owned notification policy enforced consistently at subscription and delivery; malformed/missing foreign configuration denies; redirect metadata sanitized and original row/claim/cursor preserved. CLI/tool/slash/dashboard paths qualified; TUI subscription compatibility retained. Owning commits: `674378e8597a78df67f4ff7a3dcb937262286289`, `105608061f34bd14614500143c11baba99e7fcb1`.
- **FC-19-E04**: Shared profile-owned notification policy enforced consistently at subscription and delivery; malformed/missing foreign configuration denies; redirect metadata sanitized and original row/claim/cursor preserved. CLI/tool/slash/dashboard paths qualified; TUI subscription compatibility retained. Owning commits: `674378e8597a78df67f4ff7a3dcb937262286289`, `105608061f34bd14614500143c11baba99e7fcb1`.
- **FC-19-E05**: Shared profile-owned notification policy enforced consistently at subscription and delivery; malformed/missing foreign configuration denies; redirect metadata sanitized and original row/claim/cursor preserved. CLI/tool/slash/dashboard paths qualified; TUI subscription compatibility retained. Owning commits: `674378e8597a78df67f4ff7a3dcb937262286289`, `105608061f34bd14614500143c11baba99e7fcb1`.
- **G32 / existing FC-32-E01**: all five native-thread variables are capped in a copied test-child environment; existing controller cleanup/ownership fences remain. Owning commit a87ac195800888b334a4b977d19b1419c44d99b2. No extra ledger ID.

Configured served eligibility is retained for shared listeners/cron; connected adapter coverage is reported separately. Compression publishes preserved tails atomically through existing core primitives and retains a published child if subsequent route saving fails. Delegated-child mutation refusal remains; no scratch-board escape is restored. Crypto resolver repair does not activate or restore voice.

## Validation, review and remaining qualification

56 new focused runtime cases independently passed, plus one prior compression regression. The cumulative exact-set validator passed: 78 landed IDs appear exactly once in 36 grouped human rows, all still pending. Historical 51 rows (37 old_main/14 archive_only), 239 evidence objects and 241 subfeatures remain intact. Original historical fields, baseline-source records and prior reviewed source receipts are unchanged.

Four substantial independent blocker reviews cover input isolation/metadata, served ownership/notification, text/prompts/packaging, and compression/security. Concrete findings received targeted fixes: foreign missing config denial, metadata inheritance order, MEDIA document markers, process-wrapper predicates, and explicit compression tail boundaries/transforms/growth refusal. All were accepted without recursive broad audit. Detailed original reviews are in V021_APPROVED_BATCH3_REVIEW.md; raw commands/results, final unique test selections, resolver output, official pinned OSV input proof, hashes and feature mapping are in V021_APPROVED_BATCH3_TESTS.json.

Actual pip dry-run, uv pip compile and uv lock each returned zero. Only davey changes version 0.1.4→0.1.6; all other third-party versions/artifact hashes remain. Linux CPython3.11 resolution is proven, not installation or cross-platform/voice runtime. Existing modified pytest compatibility fixtures were reviewed but not locally collected because pytest is absent and installs are excluded. Provider summaries/transports are replaced at external-effect seams; real temporary DB/config/import/handler paths are exercised. Expected negative-path logs and linked-SQLite fallback warnings remain visible in raw receipts.

Remaining approved implementation IDs: **none**. Remaining qualification: exact-candidate hosted CI and the existing deferred human checklist. No new UAT checklist or human approval gate is introduced. All workers and test subprocesses are quiescent at handoff.

## Cohesive rollback history

7bc38d6fe36f1ca079f6c41e319f816f5b7f87ac fix(kanban): validate persistent workspace metadata across write surfaces
f5c98229e3784250604f35e8cdc030ab82134aa7 fix(isolation): qualify webhook state and validate cron child boundaries
4775bf2b0396d8a224eface290744213c9ff4608 fix(gateway): publish validated connected profile coverage separately from routing
ad7e4dc28466fd134b27c86f256dfca26605f1fc fix(discord): restore literal prose and configured standalone delivery routing
49b056b85f41472cb21de9ebb54270252110fff7 fix(prompts): restore bounded model guidance and approved skill description budget
97ad45b32cbd9df946662860192aaaa446394502 fix(media): preserve original line context before protected-span masking
674378e8597a78df67f4ff7a3dcb937262286289 fix(kanban): enforce profile-owned notification policy before subscribe and delivery
3354b0a08e110a9ef067cc8beaba5aeee848e676 fix(packaging): align Discord pip and uv crypto requirements
e39c7aaefa38c2d37b730076f2da30e06ec23569 fix(kanban): validate complete legacy workspace before branch updates
105608061f34bd14614500143c11baba99e7fcb1 fix(kanban): preserve derived workspaces and deny unconfigured foreign notification owners
a87ac195800888b334a4b977d19b1419c44d99b2 fix(security): restore bounded scanner and test process contracts
71da39be8ece2dc9f8478647bee748c8563e394e fix(compression): publish preserved gateway tail in the core transaction
eef1c3a41dbdd9fc2870c4580e39e3a6181f4fa3 fix(tests): retain process guard across shell control separators
8741695f9ad9d73b770ed7e2969579d5be8e5e74 fix(media): scan standalone delivery after document marker removal
b411e475e24abd72eaae3a8810f8004a21f4660e test(media): cover document marker extraction and code-prefix isolation
6f78aabd57afc2ffd551e2855b0e9e5e81890558 fix(tests): guard shell predicates and env split-string commands
31bf95b85160ec4b205b28b01f5f4b99a2caa802 fix(compression): preserve explicit tail through adoption and commit transforms

## Pre-mutation compatible contracts (original task receipts)

---

batch3-served-contract.md

# Batch 3 served-profile compatible contract (before mutation)
FC-17-E17/E18/E29 retain Critical classification. Historical 2945588 counted successfully connected adapters and exposed multiplex coverage; current upstream deliberately uses configured served_profiles for HTTP prefixes, profile routing and cron, including adapterless profiles. Preserve that eligibility and existing allowlist/credential/listener/reconnect fences. Do not restore retired Kanban owner mixins (E19 excluded).
Publish a separate connected_profiles membership snapshot derived from successful registered adapter slots, never configured candidates. Publish after startup, reconnect, fatal removal and shutdown; clear stale membership on startup/single-profile. Runtime publication is anchored to runner launch home and restores caller ContextVar. CLI adapter coverage requires explicit connected membership AND existing live PID/start-time/home validation; older eligibility-only records never imply adapter connection. Missing/dead/stopped/malformed records fail closed. This is diagnostics, not a new routing authority or permission grant. No changes to shared cron eligibility or prompt/tool surfaces.
Use existing runtime JSON writer, profile reader and ContextVar. Tests exercise actual publication, JSON roundtrip and CLI path under temporary homes with external liveness boundaries faked. No services, live files or process mutation.

---

batch3-input-isolation-contract.md

# Batch 3 input/state isolation contract (before mutation)

FC10E02: shared webhook adapter rate buckets keyed by effective profile+route;
idempotency and delivery/session caches by profile+route+delivery. Use unambiguous
JSON tuples encoded as strings, retain raw delivery ID in HTTP/event interfaces.
Keep route-profile authorization/HMAC/body/filters unchanged; no script-mode revival.
Profile defaults follow pinned runner/adapter identity with default fallback.
FC11E08: create/update share plain-string name and strict list-of-identifier toolset
normalizers (historical grammar). None/blank names preserve fallback/clear behavior;
empty toolsets canonical None. Don't stringify nested values. Reads of old jobs
stay tolerant; changed fields validate before persistence, invalid writes preserve
bytes. Existing payload, lifecycle and owner validation stays stronger.
FC20E12: scrub every HERMES_KANBAN_* key from a copied child environment, retain
HERMES_DELEGATED_CHILD_CONTEXT and existing durable lineage/refusal checks. Do not
replace refusal with a writable scratch board or alter parent process environment.
Test actual subprocess environment path with harmless temp child, no live task writes.
Ang staging integration is authorized; workers do not push; main/live excluded.

---

batch3-kanban-contract.md

# Batch 3 compatible Kanban contracts (before mutation)

FC18E03/E04/E05: reuse current shared workspace/branch validators for ordinary create and setters. Persistent dir and worktree allow valid branch metadata; scratch rejects branches; absolute paths and required dir paths apply consistently. Dashboard forwards branch_name; CLI accepts dir branches. Preserve project-derived workspace paths, stronger decomposition overlap/child mutation guards, schema and lifecycle.

FC19E01..E05: restore shared origin/default versus telegram_home_only notification policy at subscription AND delivery. Absent policy preserves origin compatibility; present malformed configuration denies external destinations. TUI preservation and explicit allow lists remain supported. Policy uses notifier owner profile, never unrelated ambient profile; missing profile/config fails closed. Existing subscriptions remain unchanged. Redirected deliveries discard origin reply/session metadata and never wake an unrelated origin agent; cursor ownership stays with original row. CLI, tools, slash and dashboard use same resolver. Dashboard filters available homes; read-only audit reports deviations. Existing lifecycle/retry/claim behavior remains intact. Critical risk labels retained; no live delivery or state mutation.

---

batch3-text-prompts-contract.md

# Approved batch 3 text and prompt contracts (before mutation)
Risk remains Critical. Source-only, no voice behavior or live state changes.

- FC14E01: Discord converts tables as before, then renders prose markdown markers literally while preserving fenced/inline code. Restore historical escaping implementation only (no speech helpers).
- FC14E02: MEDIA delivery requires a standalone line, with optional indentation/list and existing quote wrappers. Prose mentions, code examples, blockquotes and JSON remain visible and cannot become attachments. Preserve existing protected-span masking, extension allowlist, unknown-extension real-file validation and credential denylist. Extraction and streaming cleanup share the same anchored patterns.
- FC15E01: configured auto-threading applies to free-response channels too; explicit no-thread channels, DMs, existing threads, replies and existing voice-linked exclusions remain. Failed thread creation continues to stop invocation rather than fall back into the parent channel.
- FC26-default: automatic skill descriptions use the approved 1024-character budget, preserving explicit caller budgets, truncation markers and index cache behavior.
- FC27E01/E02: exact gpt-5.6-sol and gpt-5.6-terra IDs (also one provider-qualified path ending in that exact ID) receive bounded persistence, proportional verification, and no adjacent work. Existing independent execution_guidance false/true/list/auto gates and no-tools gate remain; common mandatory-tool/verification guidance and toolset filtering stay intact. Prompt selection happens in the stable construction tier; no mid-conversation mutation.

---

batch3-compression-contract.md

# Batch 3 compression transaction contract before mutation (FC25E04)

Use existing atomic publish_compression_child/archive_and_compact; do NOT delete
an already-published child or reopen a parent across newer writes/leases.
Gateway partial compression supplies full input plus explicit preserved-tail count;
core summarizes only the requested head and rejoins tail before publication,
using the existing seam helper. All no-progress/anti-growth/lease/cancel/concurrent
append guards compare full original and final lists and stay active. Gateway
removes its redundant post-publication transcript rewrite; it adopts the already
complete canonical child. A publication failure retains parent/route and original
rows; a later routing persistence failure must not destroy child/new concurrent
writes. Existing notification finalization remains truthful on failure.
Private Python argument only, no new model-tool surface. No ordinary cached prompt
mutation outside existing compression boundary. No live migrations/restarts.
Tests real SessionDB/core publish with fake summarizer; full/partial, commit failure,
concurrent append preservation and gateway no-postpublish-rewrite path.

---

batch3-packaging-contract.md

# Batch 3 packaging contract before mutation (FC28E02)

Restore pip-compatible explicit discord.py==2.7.1,PyNaCl==1.6.2,davey==0.1.6 in
messaging extra and platform.discord lazy specs. Remove only obsolete PyNaCl uv
override; preserve Brotli,aiohttp and unrelated pins. This is approved source-only
packaging, not voice implementation or runtime activation; no parked source used.
Regenerate lock with no installation/python download, compare package versions
and hashes; actual pip dry-run and uv compile for declared lazy requirement set.
Resolver caches/reports only under task homes. Keep any unresolved platform limits
honest; no general voice runtime parity. Ang integrates; workers never push.

---

batch3-security-contract.md

# Batch 3 security/test contracts (before mutation)
FC30E01/E03/E04 retain Critical. Keep pinned upstream OSV reusable scan unconditional across repositories; only upload-sarif input uses exact NousResearch/hermes-agent predicate. Verified pinned official workflow 9a498708959aeaef5ef730655706c5a1df1edbc2 declares boolean upload-sarif and applies it only to code-scanning publication/URL, retaining artifact scan output. No hosted action executed.
Process guard: extract pure command classifier consumed by existing conftest guard. Recognize executable position through shell -c/separators, sudo option values, env unset values/assignments, timeout duration and existing launcher wrappers. Harmless echo/cat operands named skill/pkill are not executables. Block executable process-killers targeting hermes/gateway or python full-command patterns (-f, combined short f, --full). Keep current PID/subtree, systemctl, update and subprocess fences unchanged. Only classify strings in tests; never execute killers.
G32/FC32E01: per-file test child env copied then force OPENBLAS/OMP/MKL/NUMEXPR/VECLIB thread counts to 1; parent env untouched, caller large values cannot defeat ceiling; retain unique temp roots and subtree cleanup.
FC33E01: use all valid scanner findings to compute WARN suppression before display cap. Suppress only exact case-sensitive package-name pair findings from known description grammar and existing .app warnings. A BLOCK verdict and its evidence are never relaxed or filtered. Malformed result shapes/details never cause warning to become allow. Existing fail-open configuration/crash policy untouched; no scanner download/install or real scan in tests.
