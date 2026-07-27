# Provider-boundary request capture

Hermes can capture the first provider request assembled by each live agent instance for request-context diagnostics. The feature is disabled by default because the artifacts contain complete prompts, messages, and tool schemas after secret redaction.

Enable it in the active profile's `config.yaml`:

```yaml
request_capture:
  enabled: true
  retention: 20
```

Restart the CLI or gateway process after changing the setting. Each newly constructed agent writes one triplet under:

```text
<HERMES_HOME>/sessions/request-captures/
```

The triplet is published as one capture directory:

```text
<HERMES_HOME>/sessions/request-captures/capture_<id>/
  full_request.json
  no_tools.json
  raw_request.json
```

`full_request.json` contains the Hermes-visible provider request structure, including tool definitions. `no_tools.json` is derived from the same request after removing tool-definition and tool-selection fields. Both are formatted for human review. `raw_request.json` contains the same full, redacted request as valid JSON before newline expansion or line wrapping. Each file starts with a `context_summary` object covering SOUL.md, the remaining Hermes system prompt, the complete `<available_skills>` section, and tools. Tool characters are counted from compact UTF-8 JSON; `no_tools.json` reports zero tool characters. Every row reports raw characters, a provisional `characters / 3.2` token estimate, and its share of the four-section estimated total.

The two human-review artifacts are not necessarily machine-readable JSON: string values longer than 100 characters begin on the line after their opening quote, visible `\\n` text in those values is expanded into real line breaks, and long lines repeatedly wrap at the first whitespace after each 110-character span. This formatting intentionally permits invalid JSON. `raw_request.json` does not receive those transformations.

The root and capture directories are mode `0700`; artifacts are mode `0600`. All three files are written and synced in a hidden staging directory, then the directory is atomically renamed into view. A crash cannot expose a partial triplet; the next writer removes any abandoned staging directory before publishing. Cross-process writers are serialized with a crash-released OS file lock. `retention` counts complete capture directories and is clamped to `1..1000`; older captures are removed after successful publication.

## Fidelity and privacy boundary

These are provider-boundary diagnostics, not wire-level packet captures:

- They capture the request kwargs Hermes assembled immediately before provider dispatch.
- Context-summary counts are computed from those pre-redaction kwargs. Redaction can change persisted string lengths, so recounting the displayed request may differ from the summary.
- Transport-only `timeout` is omitted.
- Sensitive values are redacted before persistence. Credential-bearing fields and headers are structurally masked; URL userinfo and every URL query value are masked while preserving hosts, paths, parameter names, and separators. Persisted bytes therefore intentionally differ from the in-memory request.
- Wrappers or transformations added later by a provider SDK or remote provider are not visible to Hermes and are not included.

The `/dump-system-prompt` plugin command remains a reconstructed estimate based on the newest persisted system prompt plus current tool configuration. It is not a historical provider-request capture.
