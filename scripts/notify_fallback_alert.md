# Fallback Activation Alert Hook

`scripts/notify_fallback_alert.py` is a shell-hook consumer for the
`fallback_activated` event. It sends:

`🚨 High-priority Alert 🚨 Fallback active: <provider>/<model>`

The message is capped at 140 characters. The script pins delivery to the
default Hermes identity by running `hermes send` with `HERMES_HOME` set to the
default/root Hermes home and `HERMES_PROFILE=default`; it does not inherit the
active profile's `HERMES_*` session variables or platform credential variables.

Register it in the profile(s) that should observe fallback events:

```yaml
hooks:
  fallback_activated:
    - command: /path/to/hermes-agent/scripts/notify_fallback_alert.py
      timeout: 90
```

Approve the exact hook command with the normal shell-hook consent flow. Do not
enable `hooks_auto_accept` for this alert.

The cooldown is global across profiles and destinations:
`<default Hermes root>/state/fallback-alert-cooldown.json`. One fallback event
reserves one 300-second cooldown window before fanout starts. Suppressed events
do not extend the window. A failed or partially successful delivery remains
observable via nonzero exit/stderr and still consumes the reserved window, so a
provider storm cannot repeatedly page every home channel.
