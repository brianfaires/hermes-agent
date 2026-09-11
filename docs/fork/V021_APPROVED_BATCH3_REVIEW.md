# Batch 3 consolidated independent blocker review

Final production/test candidate `31bf95b85160ec4b205b28b01f5f4b99a2caa802`, tree `c3805ee8ae23c20ebdcaa3a25426b6f4d4e2eaa0`, based on Ang-accepted `75a30e9809eb2db9edd40fb741c6a43d717c8fb8`. All four bounded independent reviews accepted; no remaining concrete P0/P1 findings. Original reports below are reproduced verbatim; preliminary statements describe their own historical review stage and are superseded by their final acceptance sections.

Every one of the 51 changed implementation/test/compatibility-document paths matches a final accepted reviewer SHA256, verified in V021_APPROVED_BATCH3_TESTS.json. Shared gateway/slash_commands.py combines the accepted notification delta and independently accepted final compression delta. Earlier metadata bindings are superseded only by the explicitly reviewed notification/metadata correction. No earlier review is silently stretched over later changes.

Unique independently executed new cases: 56; one prior compression regression also passed. Parent checklist validator adds one, for 58 unique focused checks. Repeated intermediate cases are not added. Three actual no-install resolver commands passed and were independently inspected, not independently repeated. Full exact-candidate hosted CI remains required. Source workers do not push; Ang is authorized to integrate/push staging. Main/live excluded.

Raw outputs, final selections and exact original binding records are embedded in V021_APPROVED_BATCH3_TESTS.json. No review process or child remains running.

---

Original receipt: batch3-isolation-metadata-review.md; SHA256 9cbc0591989ceb17388751b927aa52b1708f844de9a8b709cc16fe109d746266

# Independent blocker review: batch 3 input isolation and workspace metadata

Reviewed commits `f5c98229e3784250604f35e8cdc030ab82134aa7` and `7bc38d6fe36f1ca079f6c41e319f816f5b7f87ac`; no self-review of served-profile implementation. **No concrete P0/P1 blocker identified** in these two deltas. This is bounded source/runtime review, not full CI certification.

Read pre-mutation contracts `batch3-input-isolation-contract.md` and metadata section of `batch3-kanban-contract.md`. Inspected every production/test diff in the two commits, surrounding persistence/authentication/normalizer paths, and changed webhook fixture compatibility. Exact owning commit git blobs and SHA256 plus independently tested current bytes: `batch3-isolation-metadata-review-bindings.json` (11 files). Two shared files, hermes_cli/kanban.py and plugins/kanban/dashboard/plugin_api.py, additionally carry uncommitted notification work outside this review; inspected comparison shows metadata hunks unchanged. Their current tested hashes are recorded distinctly; this review does not certify the notification additions.

## Boundary findings
- Webhook profile+route rate and profile+route+delivery identity is unambiguous JSON; HMAC/body/route-profile denial remains ahead of state allocation. Raw provider message IDs remain unchanged. Internal delivery lookup uses the same opaque key; new toolsets reader parses JSON and retains legacy route syntax. Two existing integration fixtures that inspect created delivery keys now expect new format. Other legacy-key fixtures seed delivery maps directly or construct historical session sources, so are intentionally still compatible; no blanket fixture rewrite needed. New handler test runs real HMAC check, profile denial, rate buckets, duplicate handling and background task joining. Only selected profile enumeration/agent delivery are fake.
- Cron names/toolset identifiers validate before writes on create/update; copies avoid mutating caller dictionaries. Existing read normalization stays tolerant and untouched invalid legacy fields do not block unrelated edits. Real temporary cron persistence test checks rejected updates preserve exact bytes.
- Delegation scrub returns a new environment and removes unknown future HERMES_KANBAN_* keys, while preserving delegated-child marker. Real harmless child subprocess imports and exercises the existing mutation refusal guard; parent input environment remains unchanged.
- Workspace metadata reuses existing normalizers on create/set, accepts persistent dir/worktree branch metadata and rejects scratch branch/relative or missing dir paths. Worktree project paths remain derivable later; requiring dir paths does not preclude automatic worktree project resolution. Existing write transactions and child mutation boundaries remain. Real DB and CLI/dashboard tests verify valid metadata roundtrip and invalid writes without partial mutation.

## Independent checks
`python3 ../run-approved-focused.py ../batch3-isolation-metadata-review-tests.json tests/gateway/test_v021_webhook_namespace.py tests/cron/test_v021_ancillary_validation.py tests/agent/test_v021_child_env_namespace.py tests/hermes_cli/test_v021_workspace_metadata.py`

**6 tests passed** across four files (1+1+1+3), raw command/output preserved in that JSON. Read-only canonical Python; isolated task-local homes; no installs, external services, live gateway or live DB. The known linked-SQLite fallback warning is observed, not repaired. Existing pytest-only integration fixtures inspected but not executed locally; exact candidate hosted CI still required.

No source modifications, pushes, staging/main/live mutations, or children created by review. Test runner and harmless child subprocess have exited; reviewer quiescent.

---

Original receipt: batch3-served-notification-review.md; SHA256 62718932c2e0cce54750c52870c595fc67e6be335f12022e78b8da726a4a83aa

# Independent batch 3 served ownership and notification review

Reviewer: `/root/batch3_text_prompts`, independent of served and notification implementation. No source modifications. Review limited to this substantial boundary, not prior completed features. Notification review will be appended when its candidate is frozen.

## Served coverage: no concrete P0/P1 blocker

Reviewed commit `4775bf2b0396d8a224eface290744213c9ff4608`, tree `5a37b30e03b4eb5a785ecf7628b74be957cc0393`, all five production/test diffs and immediate runtime consumers. All five files byte-match both the committed blobs and implementer SHA256 bindings. Independent binding receipt: `batch3-served-review-bindings.json`.

Contract and outcome match **FC17E17/E18/E29** with the explicitly documented compatibility qualification: configured `served_profiles` remains routing/shared-listener/cron eligibility; `connected_profiles` reports actual registered successful adapters. This avoids removing current adapterless shared-listener consumers while restoring truthful adapter diagnostics. No historical Kanban owner mixin is revived.

Publication pins runner launch home and restores the caller's home even when the writer fails. Successful reconnect publication checks running/slot ownership; fatal removal publishes before awaiting disconnect. Single-profile/startup/shutdown paths clear coverage. CLI requires both configured and connected membership plus existing live PID/start-time/home validation; malformed or old eligibility-only status cannot imply an adapter connection. Existing service status behavior remains for uncovered profiles.

Independently executed `python3 ../run-approved-focused.py ../batch3-review-served-tests.json tests/gateway/test_v021_served_coverage.py`: **10 passed**. Tests exercise real JSON writer/reader, configured-vs-connected orchestration, positive/negative reconnect ownership, fatal removal, CLI rendering, stopped/PID-reuse checks and home restoration. External connection factories and positive PID identity are faked; no live gateway or transport proof is claimed. Raw exceptions in receipt are expected negative-path fixture logging, not test failures. Canonical interpreter read-only; isolated task homes; no installation or full hosted-suite claim.

Critical classification remains. Ang must still verify the exact final candidate and require its full hosted CI.

## Notification and metadata: accepted after targeted corrections

Reviewed notification `674378e8597a78df67f4ff7a3dcb937262286289`, metadata `7bc38d6fe36f1ca079f6c41e319f816f5b7f87ac` and `e39c7aaefa38c2d37b730076f2da30e06ec23569`, and targeted correction `105608061f34bd14614500143c11baba99e7fcb1` (tree `b6b4d31e1216817e3ec6604ee8779feda449d9dd`).

Two concrete blockers were identified and corrected once: (1) existing foreign owner home without config defaulted to origin; now denies before scope hydration, preserving launch-home unconfigured compatibility; (2) required-dir validation preceded current board default-path derivation; now full validation happens after derivation, inside the insert transaction. Regression tests prove missing foreign config denial, board default inheritance, scratch non-inheritance and invalid inherited relative path rejection without writes. No remaining concrete P0/P1 blockers found.

FC18E03/E04/E05 use shared validators across CLI/API/create/setters, preserve task-owned paths and branches, and avoid partial writes. FC19E01..E05 use owner-private policy at subscription and both claim/send stages; redirects drop origin metadata and wake authority. Cursor advance/rewind/unsubscribe remain tied to original stored row. CLI audit preserves subscription rows; dashboard supports removing previously allowed subscriptions after policy restriction. Explicit exemptions and TUI preservation remain.

Independently ran `python3 ../run-approved-focused.py ../batch3-review-kanban-corrected-tests.json tests/hermes_cli/test_v021_workspace_metadata.py tests/gateway/test_v021_notification_policy.py`: **5 metadata + 12 notification = 17 passed**. Earlier notification11 run preserved in `batch3-review-notification-tests.json`; do not double count. Combined served/notification/metadata independent unique count: **27**. Real SQLite, config, parser/CLI/API handler and watcher paths run under task-local homes; only external transport sends are replaced. Expected malformed-config fixture backup occurs only in disposable home. No installed packages, actual delivery or hosted CI result claimed.

Final boundary bindings: `batch3-served-notification-review-bindings.json`, 14 reviewed paths, exact originating commit/blob/SHA256 per file. Binding snapshot HEAD `71da39be8ece2dc9f8478647bee748c8563e394e`, tree `d32b0f862cd7ee6e32bbcc8f9ae230c0b073221d`. Thirteen current files match reviewed bytes; `gateway/slash_commands.py` has a subsequent compression-only change in that HEAD, outside this review and separately assigned for review. Notification hunk remains bound to its reviewed commit. No unreviewed delta is silently covered by this report.

Review performed read-only; no source mutations for this reviewed ownership/notification boundary. Test processes exited and no child agents spawned. Separate text implementation remains this agent's ownership and is independently reviewed by the Kanban implementer, never self-approved here.

---

Original receipt: batch3-text-packaging-review.md; SHA256 f0e9a01067138ec422aef6568372effccc88b2bb3024de696a2974c7bd58d423

# Independent batch 3 text, prompts and packaging blocker review

Final disposition: **no remaining concrete P0/P1 blocker found in this assigned boundary** after one targeted text correction. This worker did not author or modify the reviewed text/prompts/packaging source/tests. Own Kanban work is reviewed by a different agent and is not covered here.

## Exact binding

Reviewed candidate checkpoint `b411e475e24abd72eaae3a8810f8004a21f4660e`, tree `f87ea8aaa871f82840d2c7bc4d38c5b028cfb716`. All **19 assigned source/test/doc files** match their committed bytes; SHA256 and git blobs are recorded in `batch3-text-packaging-review-final-bindings.json` alongside SHA256 of raw checks/resolver inputs/results.

Source commits inspected:
- `ad7e4dc28466fd134b27c86f256dfca26605f1fc`: literal Discord prose, standalone MEDIA directives, configured free-response threading and compatibility fixtures.
- `97ad45b32cbd9df946662860192aaaa446394502`: preserves original line eligibility before protected-span masking.
- `49b056b85f41472cb21de9ebb54270252110fff7`: stable bounded exact-model guidance and 1024-character skill description default.
- `3354b0a08e110a9ef067cc8beaba5aeee848e676`: explicit pip/uv-compatible Discord requirements and regenerated lock.
- Targeted correction `8741695f9ad9d73b770ed7e2969579d5be8e5e74`, regression `b411e475e24abd72eaae3a8810f8004a21f4660e`.

## Findings and targeted correction

Found a concrete native-document regression in the changed existing fixture: `extract_media('MEDIA:/tmp/report.xlsx[[as_document]]')` returned no attachment while streaming cleanup removed the entire directive. Original reproduction is `batch3-review-media-marker-reproduction.json` (canonical isolated interpreter, assertion failed). The newly anchored regex was applied to original text before the document marker was removed.

Owner corrected only the scan input to the already marker-cleaned string, then added a dedicated runtime test asserting exact returned attachment/text plus a code-prefix negative. Reviewer inspected that delta and independently reran the final text file: **8 passed**. Original-line eligibility, code/JSON/blockquotes, adjacent-tag rejection, complete unknown-extension validation and delivery path checks remain. No recursive broad audit or unrelated source change requested.

## Runtime evidence independently executed

Using canonical read-only Python through `../run-approved-focused.py`, task-local sanitized HOME/HERMES_HOME/HERMES_BUNDLES_DIR/TMPDIR and source PYTHONPATH:

- `python3 ../run-approved-focused.py ../batch3-independent-text-packaging-tests.json tests/gateway/test_v021_text_compatibility.py tests/agent/test_v021_prompt_contracts.py tests/tools/test_v021_discord_packaging.py`: original **7 text + 3 prompt + 1 packaging passed**.
- After correction, `python3 ../run-approved-focused.py ../batch3-independent-text-correction-tests.json tests/gateway/test_v021_text_compatibility.py`: final **8 text passed**.
- Unique final reviewed runtime cases: **12**, not 19 (repeated text checks are not double counted).
- `git diff --check` passed. No running test process remains.

Actual methods exercised include Discord formatter and message handler with fake transport/thread creation; BasePlatformAdapter extraction and cleanup with real temporary unknown-extension file; stable prompt builder with gate/tool/model combinations and repeated equality; skill description helpers; actual lazy installer command construction and manifest parsing. No network delivery/model calls or running gateway.

## Source conclusions and qualification limits

- FC14E01/E02 and FC15E01: existing nonvoice formatting and routing primitives reused. Tables convert before literal prose escaping; fenced/inline code remains. Standalone MEDIA policy is consistent after marker correction. Explicit no-thread, disabled automatic threading, DMs, existing threads/replies and pre-existing voice-linked exclusions remain in code; no voice implementation restored. Failed thread creation still prevents inline agent fallback.
- FC26-default and FC27E01/E02: larger default description budget preserves explicit limits and truncation signal. Exact sol/terra IDs with at most one provider segment receive bounded persistence; near-match collisions retain default guidance. Existing execution-guidance true/false/custom-list/auto and no-tools gates remain independent. Injection occurs in stable construction; no mid-conversation cache/toolset mutation.
- FC28E02: lazy and manifest specifications match. Parent's three actual no-install commands (pip dry-run, uv pip compile, uv lock) each returned zero; reviewed raw output in `batch3-resolver-tests.json`, not independently rerun on network. Independently checked resolver input equals manifest subset, resolver report versions and wheel SHA256 match lock for discord-py/PyNaCl/davey/Brotli/aiohttp. Independently parsed old/new lock: package names unchanged; changed records exactly davey, discord-py and hermes-agent; every other third-party package record including artifact hashes unchanged. Only davey version changes 0.1.4→0.1.6; Discord record drops unused voice-extra dependency metadata. PyNaCl uv-only override removed while unrelated constraints remain.

Existing edited pytest compatibility files were inspected but pytest collection was not run locally (no installation permitted); exact candidate hosted CI remains required. Resolver evidence covers Linux CPython3.11 wheels and lock resolution, not installation/runtime or all OS/architecture combinations. No claim of full hosted CI green or live/voice parity. Critical risk labels retained. Worker source push is excluded; Ang's staging integration is authorized; main/live remain excluded. Previous reviewed receipts untouched.

---

Original receipt: batch3-compression-security-review.md; SHA256 fca991a3eb86c73a688c657fed4880e5b295f2883430e3e2f6d938dc35be7144

# Independent batch 3 compression and security review

Reviewer `/root/batch3_text_prompts`; source read-only for this boundary. Review covers parent compression and separate security implementation, not this reviewer's text/prompt changes. Exact final bindings and qualified tests will be appended after targeted fixes.

## Compression findings against 71da39be8ece2dc9f8478647bee748c8563e394e

The change removes gateway's destructive second transcript rewrite and places full partial-compression publication under existing core transaction, lease, cancellation and growth guards. Concrete preserved-tail gaps identified within that change:

1. Adopting a longer durable parent shifts a fixed last-N boundary, allowing the caller's original explicitly preserved tail into the summarizer head.
2. Post-rejoin mechanical growth salvage strips older reasoning/tool bodies from the complete candidate, including explicitly preserved tail rows. The same issue class includes todo refresh mutating a preserved final user row after early rejoin.

Requested one targeted correction preserving the caller boundary (or refusing ambiguous adoption), refusing growth rather than salvaging explicit tails, and preventing post-summary transforms from altering the explicit suffix. Parent owns correction and runtime regressions. No production writes by reviewer.

## Security preliminary review

Reviewed a87ac195800888b334a4b977d19b1419c44d99b2 and eef1c3a41dbdd9fc2870c4580e39e3a6181f4fa3. Scanner WARN-only exact-package suppression preserves case/punctuation distinction, BLOCK verdicts, malformed evidence and full finding-set decision before display cap. Pinned workflow content confirms boolean upload-sarif input gates code-scanning publication while scan and artifact steps remain. Test-child native thread limits use copied env and preserve controller state.

Concrete pure-classifier gap: `if pkill -f hermes; then echo done; fi`, `bash -c 'while pkill -f hermes; do :; done'`, and argv `env '--split-string=pkill -f hermes'` returned False. No killer was executed: only `is_process_killer` was called on literal strings/argv. Requested targeted control-condition-prefix and equals-form split-string handling, preserving harmless echo/cat operands. Parent assigned security implementer correction. This is bounded executable-position guarding, not a full shell interpreter claim.

## Security final acceptance

Targeted correction `6f78aabd57afc2ffd551e2855b0e9e5e81890558` closes reported executable-position gaps with if/elif/while/until prefixes and both separate/equals env split-string forms (including trailing argv). Expanded harmless echo/cat controls remain permitted. No remaining concrete P0/P1 blocker in reviewed security boundary.

Independently ran `python3 ../run-approved-focused.py ../batch3-review-security-final-tests.json tests/tools/test_v021_security_contracts.py`: **6 tests passed**, including24 dangerous classifier literals and14 harmless cases, actual copied-child environment propagation without real signals, WARN/BLOCK exact/distinct/cap/malformed scanner matrices. No kill command or paid service executed. Canonical interpreter read-only with task-local home.

All six security files match final exact SHA256/git blobs in `batch3-security-independent-bindings.json`; originating source commits a87ac195800888b334a4b977d19b1419c44d99b2, eef1c3a41dbdd9fc2870c4580e39e3a6181f4fa3, correction6f78aabd57afc2ffd551e2855b0e9e5e81890558. The read-only pinned OSV upstream receipt SHA256 ed3830519b9ffc799eafa57fd3bc9f3e0b3c8fe000542c4ebbc5654a92d3f92a was inspected: upload-sarif boolean gates only Code Scanning publication; scan/artifact paths remain. No current hosted CI result inferred.

## Compression final acceptance

Targeted correction `31bf95b85160ec4b205b28b01f5f4b99a2caa802`, tree `c3805ee8ae23c20ebdcaa3a25426b6f4d4e2eaa0`, resolves the reported preservation gaps. It captures the original caller boundary before durable adoption and checks semantic prefix compatibility (ignoring persistence markers); additional adopted durable messages stay in the preserved suffix. Todo/user-turn transformations now operate only on the summarized head before final seam rejoin. Explicit-tail overgrowth refuses publication instead of applying lossy generic salvage. No remaining concrete P0/P1 blocker found in this bounded compression review.

Independently executed `python3 ../run-approved-focused.py ../batch3-review-compression-final-tests.json tests/agent/test_v021_compression_publication.py tests/agent/test_v021_compression_tail_guards.py tests/gateway/test_v021_compression_adoption.py tests/gateway/test_v021_compression_failure.py`: **3 publication + 3 tail guards + 2 gateway adoption + 1 prior failure = 9 passed**. Each file ran in its own sanitized temporary home with canonical interpreter, below its60-second deadline. This includes8 new tests and1 prior regression; do not count the prior regression as newly added.

Real SessionDB tests cover atomic rotating/in-place tail publication, parent commit failure preserving source rows, concurrent child appends surviving, concurrent parent growth preserving original caller tail, growth refusal without salvage, and todo refresh keeping final user text intact. Gateway tests use actual leased SessionDB child publication and route-save failure, and assert no destructive second rewrite. Summarization/provider calls are faked; no live model provider or gateway process is exercised. Existing lease/cancellation/supersession/no-op/anti-growth guards remain in their admitted commit sequence. Plugin compressors still receive only existing supported kwargs; preserve_tail_count is private AIAgent/core forwarding and does not widen plugin/model-tool schemas. Inspected affected gateway fake call patterns accept keyword arguments; full hosted suite is still required.

Six final compression source/test files byte-match committed blobs and SHA256 in `batch3-compression-independent-bindings.json`; includes the compression change to `gateway/slash_commands.py` explicitly excluded from earlier served/notification binding. Together with the reviewed notification source commit this covers both boundary changes to that shared file. Security's separate six-file final receipt remains `batch3-security-independent-bindings.json`.

Combined independent compression/security count **15 tests**, of which14 new and1 prior regression. No source writes by this reviewer in either reviewed boundary. All review test processes have exited; no child agents spawned. Prior receipts remain immutable. Ang owns staging integration and exact-candidate hosted CI; no CI green claim, live changes, installs or push performed.

---

# Independent final batch 3 handoff metadata review

**Accepted: no concrete documentation blocker found.** This was a bounded consolidation/binding check only; no new source audit, source edit, runtime test, installation, network call or child agent.

Read `docs/fork/V021_APPROVED_BATCH3.md`, `V021_APPROVED_BATCH3_REVIEW.md`, parsed the complete `V021_APPROVED_BATCH3_TESTS.json`, and checked `../batch3-integrity.json` against accepted original receipts. Source candidate `31bf95b85160ec4b205b28b01f5f4b99a2caa802` resolves to documented tree `c3805ee8ae23c20ebdcaa3a25426b6f4d4e2eaa0`; accepted base is `75a30e9809eb2db9edd40fb741c6a43d717c8fb8`. The later documentation commit is correctly deferred to the external final evidence receipt rather than self-referenced.

- Exactly the base ledger's **26 approved pending IDs** appear in the bundle feature map and are now locally qualified; remaining approved pending IDs are empty. G32 is completed inside existing FC-32-E01, with Critical risk and no extra historical ID.
- Final selected raw results reconcile to **58 unique focused checks = 56 new + one prior compression regression + one cumulative checklist validator**. Every selected entry matches an original receipt entry, returned zero, and its `Ran N tests` count matches integrity. Intermediate repeated cases are not double counted. Four accepted substantial review reports are reproduced verbatim with matching SHA256; preliminary findings are expressly superseded by final correction acceptance.
- All **17 embedded original receipt objects and SHA256** match the external originals. All **51 changed source/test/compatibility-document paths** exactly match the base-to-source-candidate changed-path set; each final SHA256 and git blob matches an accepted review binding and current bytes. Shared slash-command coverage uses the final compression binding plus documented earlier notification review.
- Historical **51 rows / 37 old_main / 14 archive_only / 239 evidence objects / 241 subfeatures** are preserved by the recorded integrity check; independently compared historical fields and baseline-source records without changes. All **10 prior source receipts** match accepted-base bytes. Cumulative checklist receipt reports **78 landed IDs / 36 grouped human rows, all pending**; no additional human checklist or approval gate is introduced.
- CI language is accurate: first-batch **34545497788 failed**; Ang accepted the correction and reported fresh preceding-candidate CI starting; no green result is asserted. Exact final-candidate hosted CI remains required.
- Authorization language correctly distinguishes **Ang's authorized staging integration/push** from **workers' no-push/no-staging-mutation** restriction. Main/canonical/live, voice/retired/archive-only, installs and real process-killer execution remain excluded. Packaging claims stay limited to three successful no-install resolver commands, independently inspected rather than network-rerun, with Linux CPython3.11 and no voice-runtime assertion.

Only this new external metadata review receipt was written. Prior receipts remain untouched. Reviewer is quiescent with no test process or child agent running.
