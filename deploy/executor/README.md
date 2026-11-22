# Stable executor renewal (example)

`tessera-executor-renew.service` and `.timer` renew a stable executor enrollment
(`scripts/executor_enrollment.py`, see
[provider trust: stable executor](../../docs/provider-trust.md#stable-executor-route-setups-that-mint-principals))
twice a day. Control caps each worker credential at 24 hours and never revives an expired
one, so a missed renewal means rotation. Renewal keeps the same principal and token and
stops at the enrollment's `not_after` (at most seven days after enrollment); the unit then
fails and its log names the reason, and the operator rotates: enroll a new directory, list
its principal in the provider trust file, reload, point routes and the campaign executor at
it, then `revoke` the old directory.

Install as the service user (paths are placeholders):

```sh
cp tessera-executor-renew.service tessera-executor-renew.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tessera-executor-renew.timer
journalctl --user -u tessera-executor-renew.service   # identifiers and expiry only
```

Use `loginctl enable-linger USER` so user timers run without a login session.
