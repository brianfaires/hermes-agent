# Provider-boundary request capture

Hermes can capture the first provider request assembled by each live agent instance for request-context diagnostics. The feature is disabled by default because the artifacts contain complete prompts, messages, and tool schemas after secret redaction.

Enable it in the active profile's `config.yaml`:

```yaml
request_capture:
  enabled: true
  retention: 20
```

Restart the CLI or gateway process after changing the setting. Each newly constructed agent writes one pair under:

```text
<HERMES_HOME>/sessions/request-captures/
```

The pair is published as one capture directory:

```text
<HERMES_HOME>/sessions/request-captures/capture_<id>/
  with_tools.json
  prompt_only.json
```

`with_tools.json` contains the Hermes-visible provider request structure, including tool definitions. `prompt_only.json` is derived from the same request after removing tool-definition and tool-selection fields.

The root and pair directories are mode `0700`; artifacts are mode `0600`. Both files are written and synced in a hidden staging directory, then the directory is atomically renamed into view. A crash cannot expose a half-pair; the next writer removes any abandoned staging directory before publishing. Cross-process writers are serialized with a crash-released OS file lock. `retention` counts complete capture directories and is clamped to `1..1000`; older captures are removed after successful publication.

## Fidelity and privacy boundary

These are provider-boundary diagnostics, not wire-level packet captures:

- They capture the request kwargs Hermes assembled immediately before provider dispatch.
- Transport-only `timeout` is omitted.
- Sensitive values are redacted before persistence. Credential-bearing fields and headers are structurally masked; URL userinfo and every URL query value are masked while preserving hosts, paths, parameter names, and separators. Persisted bytes therefore intentionally differ from the in-memory request.
- Wrappers or transformations added later by a provider SDK or remote provider are not visible to Hermes and are not included.

The `/dump-system-prompt` plugin command remains a reconstructed estimate based on the newest persisted system prompt plus current tool configuration. It is not a historical provider-request capture.
