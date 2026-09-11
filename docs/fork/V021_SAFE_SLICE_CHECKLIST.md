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
credential or data mutation or main promotion. Workers do not push; Ang is
authorized to integrate and push staging. Voice restoration, retired
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
| Test-runner cleanup — `FC-32-E01` | Run a harmless disposable test child under oversized native-thread environment values, then force spawn failure → child BLAS/OMP/MKL/NUMEXPR/VECLIB counts are 1 and allocated failed-spawn temporary root is removed. | Parent environment and unrelated temporary roots/files survive. | pending / — |
| Gateway restart delivery barrier — `FC-36-E02`, `FC-36-E03`, `FC-36-E04`, `FC-36-E05`, `FC-36-E06`, `FC-36-E07` | In an authorized disposable gateway, request model-tool, ordinary slash and busy-inline slash restart while delaying caller response delivery → restart waits for the correct generation's delivery before draining/reconnecting intended profiles, even after active agent counts reach zero. | Denied/unauditable requests never restart; unrelated or stale callbacks cannot release the barrier. Use inert supervisor boundaries until disposable restart is separately authorized. | pending / — |

| Launch and runtime identity — `FC-17-E03`, `FC-17-E06`, `FC-17-E24` | Construct a gateway/adapter for A, enter ambient B, and submit unrouted and explicitly routed fixture messages → A stays the default; valid routes select their named runtime; equal home reuses a stable cache signature. | Missing named runtime refuses; differing homes cannot share cache identity; owner is stamped before busy/session routing and transport references are not serialized. | pending / — |
| Adapter durable home — `FC-17-E04` | Construct Discord adapter A then trigger tracker, command-sync and pairing operations under ambient B → only A's disposable state paths are used. | B's state and pairing grants cannot be read or changed by those operations. | pending / — |
| Transport delivery and pairing — `FC-17-E05`, `FC-17-E20`, `FC-17-E21`, `FC-17-E22` | Receive through A while runtime routes to B; exercise delivery selection, DM policy, pairing approval/request and rate-limit → all transport authorization follows A. | Stopped/unregistered retained transport refuses fallback to B; absent secondary store cannot borrow default grants or policy. | pending / — |
| Delayed Discord controls — `FC-17-E23` | Open execution approval, slash confirmation, update prompt, model picker, choice picker and clarify choice from A; interact after entering B → each uses A's captured pairing home and authorization settings. | B's grants/allow-all never authorize A's control; existing user, role and admin restrictions still apply. | pending / — |
| Private cron credentials — `FC-17-E14`, `FC-17-E15`, `FC-17-E16` | Rotate disposable per-profile credentials; run direct cron jobs, concurrent scripts and a destination-profile bot fixture → each refreshes privately, child environments use the correct profile, and caller scope is restored. | No process environment mutation, cross-profile secret borrowing or provider-secret escape through script sanitizer; missing destination and unscoped multiplex refuse. Real mounted-root bot CLI startup remains a separate pending UAT check. | pending / — |
| Durable session ownership and resume — `FC-17-E25`, `FC-17-E26`, `FC-17-E30` | Recover and resume owned legacy/current fixture sessions and compression ancestry → route, reopen/reset and peer metadata commit together before memory publication. | Foreign or missing target/ancestor and injected SQL failure leave rows/route unchanged; ownerless multiplex refuses, stale snapshots cannot overwrite, and mirror failure does not undo committed DB state. | pending / — |

| Webhook profile state — `FC-10-E02` | Deliver identical route/provider IDs in two disposable profiles, rebind a route, and retry → profiles/routes have independent rate, idempotency and cached destinations; same-owner retry is duplicate. | Wrong profile/HMAC refuses before consuming state; raw provider ID remains intact; route toolsets still resolve for current and legacy source IDs. | pending / — |
| Cron ancillary validation — `FC-11-E08` | Create/update plain names and toolset lists → names trim and lists normalize/deduplicate through all write surfaces. | Nested/nonstring/invalid values refuse without changing stored bytes; untouched legacy records still load. | pending / — |
| Literal text and standalone media — `FC-14-E01`, `FC-14-E02` | Send Discord prose/table/code and standalone MEDIA directives, including a document marker and a real spaced filename → prose stays literal, code stays intact, supported directives deliver and disappear from display consistently. | Prose/JSON/code/blockquote/glued tags never deliver; missing or denied files remain protected; marker removal cannot promote inline code examples. | pending / — |
| Free-response threading — `FC-15-E01` | Enable automatic threading for a disposable free-response channel → response runs in the created thread. | Explicit no-thread/disabled settings stay inline; failed thread creation cannot invoke the agent inline or bypass existing reply/DM/channel restrictions. | pending / — |
| Served-profile ownership and status — `FC-17-E17`, `FC-17-E18`, `FC-17-E29` | Start mixed successful/failed/adapterless fixture profiles, reconnect and stop → launch-owned runtime publishes connected membership and CLI reports only validated connected coverage. | Stale/dead/malformed/wrong-home records refuse coverage; late reconnect after shutdown cannot publish. Configured shared routing/cron eligibility survives independently. | pending / — |
| Kanban workspace metadata — `FC-18-E03`, `FC-18-E04`, `FC-18-E05` | Create/edit dir and worktree branch metadata through CLI/API/DB, including inherited board workdir → normalized complete metadata persists and dashboard forwards branch. | Scratch branches, invalid refs/paths and invalid legacy branch updates fail atomically; valid board inheritance remains supported. | pending / — |
| Kanban notification policy — `FC-19-E01`, `FC-19-E02`, `FC-19-E03`, `FC-19-E04`, `FC-19-E05` | Exercise origin/deny/Telegram-home policy via CLI/tool/slash/dashboard subscriptions and pending delivery → one owner-profile resolver selects or refuses destination, preserving TUI subscriptions and policy exemptions. | Missing/malformed foreign config denies; redirects cannot retain origin reply/wake metadata; failed or newly denied delivery rewinds the original cursor without deleting subscriptions. Existing denied subscriptions can still be removed. | pending / — |
| Complete child environment scrub — `FC-20-E12` | Launch a harmless delegated child with known and invented worker keys → all HERMES_KANBAN_* keys are absent and unrelated values survive. | Parent environment remains intact; delegated marker still makes direct durable Kanban mutation refuse, with no writable scratch-board bypass. | pending / — |
| Atomic compression publication — `FC-25-E04` | Partially compress a disposable session while adding newer parent/child turns → requested tail and new rows survive one atomic publication and gateway adopts without rewriting. | Commit failure retains parent; ambiguous boundary or growth refuses; todo/salvage cannot alter preserved suffix; later route-save failure never deletes canonical child or reopens its parent. | pending / — |
| Prompt description and execution budgets — `FC-26-default`, `FC-27-E01`, `FC-27-E02` | Build a fresh prompt with long skill descriptions and exact sol/terra variants → default description budget is 1024, explicit bounds remain, and bounded guidance follows configured gates. | Similar model names do not match; empty tools/disabled gate stay excluded; repeat builds and existing conversation prompts remain stable. | pending / — |
| Pip-compatible Discord packaging — `FC-28-E02` | Inspect candidate manifest/lazy commands and resolve explicit requirements in a disposable no-install resolver → pip and uv agree on Discord/PyNaCl/Davey pins and current aiohttp/Brotli. | No implicit incompatible voice extra or uv-only crypto workaround; no live install, parked voice source or unsupported runtime-parity claim. | pending / — |
| Test process classifier — `FC-30-E01` | Feed executable/wrapper/control-flow literals and harmless operands to the pure guard → prohibited process targets block and innocent echo/cat text remains allowed. | Never execute those killer strings; env split-string, shell conditions and full-python patterns cannot bypass; ancestor/foreign PID restrictions remain. | pending / — |
| OSV publication policy — `FC-30-E03`, `FC-30-E04` | Inspect/evaluate workflow inputs for upstream and fork fixtures → scanning/artifacts stay enabled everywhere, code-scanning publication is upstream-only. | Fork publication is suppressed without suppressing scan; no hosted success inferred from local checks. | pending / — |
| Tirith complete finding verdict — `FC-33-E01` | Feed exact-package warning, mixed warnings beyond display cap, malformed findings and blocking verdicts → only justified warnings suppress; full finding set determines verdict before display cap. | BLOCK and evidence never relax; near-match package names, unknown/malformed details and nonsuppressible findings retain warning. | pending / — |

## Retained checks carried forward from the earlier checkpoint

These preserve the five earlier human checks; they are not newly restored features.
Use the same candidate/operator/evidence fields and disposable-activation gate above.

| Retained feature | Action → expected result | Safety negative | Status / evidence |
| --- | --- | --- | --- |
| Delayed model picker | Select a model for a named profile after a delay → only that profile's settings change. | Other profile settings remain unchanged. | pending / — |
| Secondary cron coverage | Run an allowlisted secondary-profile cron fixture → its own heartbeat and adapter identity appear. | Excluded profiles remain inactive. | pending / — |
| Secondary-only notification transport | Use a platform connected only for the intended secondary profile → its permitted task notification delivers through that profile. | No default-profile fallback or notification-policy bypass. | pending / — |
| Shared restart preflight | Inspect active work before the restart-barrier check above → all active work appears, and restart waits for caller delivery before intended-profile drain/reconnect. | Do not execute a real restart without separate disposable activation authorization. | pending / — |
| Linked-worktree setup safety | Exercise setup in a disposable linked-worktree fixture → canonical launcher and shell configuration remain unchanged. | Do not run bootstrap or repair against the live installation. | pending / — |

## Candidate and rollback boundaries

Implementation/actual-pytest checkpoint: `38c54fbb1b733e6c98e93f9e03f3332f8f5e974f`.
This checklist-only follow-up does not change its source/test bytes. The exact
integrated commit/tree and fresh full CI are bound in Ang's task handoff and the
commit-specific link to this file; record that final identity when testing.

Rollback is not authorized here. Use the ledger's owning commits, keeping each
feature and its corrective commits/tests together. In particular: registry and
release-observer compatibility travel together; restart delivery/audit/drain
fixes travel together; credential/transport/ownership changes require coordinated
rollback; compression publication and tail-preservation corrections stay together.
Keep manifest and lockfile together for packaging. Do not withdraw only a safety
correction or restore known failed fixtures. Preserve historical evidence; any
actual activation or rollback needs an explicit environment and verified target.

Historical CI at earlier candidates remains historical. Only the final exact-SHA
CI receipt qualifies this cumulative staging candidate; human checks stay pending.
