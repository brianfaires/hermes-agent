# Cumulative restoration human checklist

All human checks are **pending**. Brian approved the documented remaining
restoration for source/tests/staging on 2026-09-10; human testing is deferred
and does not gate development. This replaces the partial milestone checklist
and stale implementation approval holds. Critical risk classifications remain
in [critical-gates.md](critical-gates.md). Automated inventory and receipts live
in the restoration ledger and qualification documents, separately from this list.

Candidate binding: each result must record the exact tested Git commit and tree,
matching source checkout, disposable environment, operator/date, and evidence
location. The ledger's `source_candidate` identifies its source checkpoint; the
final task handoff binds any later documentation commit. No human pass is claimed
for either checkpoint. Change a row's status to **pass** or **fail** only with
that evidence; otherwise retain **pending**. Every ID in a grouped row is covered
by its action, expected result and safety negative.

Run these only in a separately authorized disposable deployment with synthetic
profiles/data and inert external boundaries where practical. This continuation
authorizes no live activation, restart, delivery, install, configuration,
credential or data mutation, main promotion or push. Voice restoration, retired
consumers and archive-only new features stay excluded. As further approved
subfeatures land, add their IDs and checks here in the same change as the ledger;
approval alone is not implementation evidence.

| Feature / landed subfeature IDs | Human action → expected result | Safety-sensitive negative | Status / candidate-bound evidence |
| --- | --- | --- | --- |
| TTS provider settings — `FC-04-E01`, `FC-04-E02` | In synthetic config, set bounded ElevenLabs voice controls and exercise normal and streaming synthesis → both use the configured values; unset settings preserve provider defaults. | Simulate unavailable SDK or failed synthesis → response text remains visible; no credential appears in output. | pending / — |
| Cleanup — `FC-08-E01` | Preview and run cleanup on disposable aged/fresh wildcard fixtures → only eligible tracked files are removed. | Legacy directories, fresh files, symlinks and untracked descendants survive. | pending / — |
| Webhook transform cleanup — `FC-10-E03`, `FC-30-E02` | Run a disposable JSON transform, then a deliberately timed-out child/grandchild fixture → success returns JSON; timeout leaves no fixture descendants. | Unrelated processes survive; this does not enable retired script-trigger mode. | pending / — |
| Cron alert rendering — `FC-12-E01`, `FC-12-E02`, `FC-12-E03` | Produce a synthetic warning, failure and mismatched same-chat thread delivery → attention markers and relevant thread diagnostic appear. | A different platform/chat does not produce the same-thread warning; rendering does not reroute the destination. | pending / — |
| File-backed cron prompts — `FC-13-E01`, `FC-13-E02`, `FC-13-E03`, `FC-13-E04`, `FC-13-E05` | Create/update file-backed jobs through tool and API, then edit the file before a disposable run → effective inline+file content reloads and is validated at each entrypoint. | Unsafe combined content is rejected even with skills; relative/missing/oversized/invalid UTF-8 files and invalid updates leave stored jobs unchanged. | pending / — |
| Compact progress and Discord embeds — `FC-14-E03`, `FC-14-E04`, `FC-14-E05` | Show a long path and shell prologue in progress; exercise Discord send/edit/forum/overflow fixture paths → a bounded meaningful preview appears and progress embeds are suppressed. | Executed command arguments remain intact; existing channel policy still refuses prohibited destinations. | pending / — |
| Profile cron registry and recovery — `FC-17-E12` | In two disposable profiles, dispatch equal job IDs, release one, then simulate stale/finished work and a drain → each profile runs/releases/reconciles only its own job; global drain counts both. | A live peer in another profile neither blocks stale recovery nor gets released/interrupted by a different owner; finite one-shots keep live records and exact owner fencing. | pending / — |
| Discord channel policy precedence — `FC-17-E09` | Load synthetic top-level/nested channel settings, then compare pre-connect/standalone sends with connected snapshots → explicit allow/deny config (including empty values) wins before connection; connected nonempty profile snapshot remains authoritative. | Conflicting environment cannot bypass explicit denial; scoped misses never borrow another profile's environment, ignored channels still deny, and thread parent identity remains verified. | pending / — |
| Kanban board inventory — `FC-18-E01`, `FC-18-E02` | List two disposable boards while a task DB pin names only one → each inventory row reports its own canonical DB path and counts. | Inventory is read-only and neither database changes. | pending / — |
| Kanban notification diagnostics — `FC-19-E07` | Inject notification delivery failure → operator diagnostics include the exception traceback. | No destination or subscription policy changes merely to recover delivery; diagnostic evidence contains no credentials. | pending / — |
| Delegated child lifecycle isolation — `FC-20-E06`, `FC-20-E07`, `FC-20-E08`, `FC-20-E09`, `FC-20-E11` | In a disposable worker task, compare parent with delegated child/cron context through initialization, stop/finalize and activity reporting → only the owning parent supplies worker guidance and lifecycle/activity updates. | Child cannot nudge, finalize or mutate the parent's board, nor acquire worker-only skills; existing conversation prompt bytes remain unchanged. | pending / — |
| Compression bookkeeping — `FC-25-E05` | Simulate manual compression returning neither rotation nor in-place publication → operation reports failure and retains token/bookkeeping state. | No success/committed status for an unpublished result; this row does not claim published-child rollback coverage. | pending / — |
| External skill cache and descriptions — `FC-26-E01`, `FC-26-E02` | Edit/add/delete an external skill and change its category; build a fresh prompt and request explicitly bounded descriptions → new builds reflect files/category and honor the requested bound. | An already-running conversation's prompt stays byte-stable; this row does not claim a changed automatic description default. | pending / — |
| Core dependency floors — `FC-28-E01` | Inspect the exact candidate's source dependency resolution for supported core paths → Starlette and multipart satisfy the restored security floors. | Do not infer voice dependency/runtime qualification or install into a live environment. | pending / — |
| Test-runner cleanup — `FC-32-E01` | Force test subprocess spawn failure in a disposable harness → failure is reported and its allocated temporary root is removed. | Unrelated temporary roots/files survive. | pending / — |
| Gateway restart delivery barrier — `FC-36-E02`, `FC-36-E03`, `FC-36-E04`, `FC-36-E05`, `FC-36-E06`, `FC-36-E07` | In an authorized disposable gateway, request model-tool, ordinary slash and busy-inline slash restart while delaying caller response delivery → restart waits for the correct generation's delivery before draining/reconnecting intended profiles, even after active agent counts reach zero. | Denied/unauditable requests never restart; unrelated or stale callbacks cannot release the barrier. Use inert supervisor boundaries until disposable restart is separately authorized. | pending / — |

| Launch and runtime identity — `FC-17-E03`, `FC-17-E06`, `FC-17-E24` | Construct a gateway/adapter for A, enter ambient B, and submit unrouted and explicitly routed fixture messages → A stays the default; valid routes select their named runtime; equal home reuses a stable cache signature. | Missing named runtime refuses; differing homes cannot share cache identity; owner is stamped before busy/session routing and transport references are not serialized. | pending / — |
| Adapter durable home — `FC-17-E04` | Construct Discord adapter A then trigger tracker, command-sync and pairing operations under ambient B → only A's disposable state paths are used. | B's state and pairing grants cannot be read or changed by those operations. | pending / — |
| Transport delivery and pairing — `FC-17-E05`, `FC-17-E20`, `FC-17-E21`, `FC-17-E22` | Receive through A while runtime routes to B; exercise delivery selection, DM policy, pairing approval/request and rate-limit → all transport authorization follows A. | Stopped/unregistered retained transport refuses fallback to B; absent secondary store cannot borrow default grants or policy. | pending / — |
| Delayed Discord controls — `FC-17-E23` | Open execution approval, slash confirmation, update prompt, model picker, choice picker and clarify choice from A; interact after entering B → each uses A's captured pairing home and authorization settings. | B's grants/allow-all never authorize A's control; existing user, role and admin restrictions still apply. | pending / — |
| Private cron credentials — `FC-17-E14`, `FC-17-E15`, `FC-17-E16` | Rotate disposable per-profile credentials; run direct cron jobs, concurrent scripts and a destination-profile bot fixture → each refreshes privately, child environments use the correct profile, and caller scope is restored. | No process environment mutation, cross-profile secret borrowing or provider-secret escape through script sanitizer; missing destination and unscoped multiplex refuse. Real mounted-root bot CLI startup remains a separate pending UAT check. | pending / — |
| Durable session ownership and resume — `FC-17-E25`, `FC-17-E26`, `FC-17-E30` | Recover and resume owned legacy/current fixture sessions and compression ancestry → route, reopen/reset and peer metadata commit together before memory publication. | Foreign or missing target/ancestor and injected SQL failure leave rows/route unchanged; ownerless multiplex refuses, stale snapshots cannot overwrite, and mirror failure does not undo committed DB state. | pending / — |

Historical CI: exact `3c8f7d0` passed hosted run 34523897060, including 52 added
tests. This remains a historical checkpoint, not evidence for later candidates.
Future integration and full CI belong to Ang's exact-SHA staging verification.
