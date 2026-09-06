# Shared gateway restart

Opt-in plugin for authorized messaging profiles. Enable the existing
`gateway-restart-tool` plugin and `gateway_restart` toolset through the normal
profile configuration. It adds no core tool and has no profile-target parameter:
a multiplex gateway restarts as one process.

`request_gateway_restart` requires a nonempty reason and the exact confirmation
`restart gateway`. `dry_run: true` reports current work and supervisor mode.
Actual requests require a currently running messaging conversation in the live
gateway and that profile's plugin enablement. CLI, cron, API and Kanban workers
cannot use it to replace the gateway lifecycle controls.

The plugin writes intent to the gateway owner's
`logs/gateway-restart-tool.jsonl` before scheduling and shares
`.gateway_restart_tool_state.json` across profiles. Existing single-timestamp
and legacy per-profile timestamp state are readable without migration.
`plugins.entries.gateway-restart-tool.cooldown_seconds` defaults to 300.
An ambiguous scheduling failure retains the cooldown: inspect status before
retrying. Audit/state failure prevents unaudited scheduling.

The current `GatewayRunner.request_restart` owns admission, after-turn waiting,
cron/API work accounting, drain and teardown. Configured upstream safety caps
still apply; the plugin does not promise an unlimited drain. Explicit external
operator drains reject a restart request. This is a restart tool, not a source
switch or release controller; it does not run Git, installers, signals or service
commands.
