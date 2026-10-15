# mobile-cli — `hermes mobile`

Pairs a mobile Hermes client (Hermes Console) with **this** install: it reports
what the phone needs, then renders the `hermes://pair` payload as a QR code.

```bash
hermes mobile                 # state + QR to scan (needs plugins.enabled: mobile-cli)
hermes mobile --json          # machine-readable state — no key, no code
hermes mobile --link          # print the pairing link instead of the QR (opt-in)
hermes mobile --label "Em"    # override the instance name the app shows
hermes mobile --help
```

## Where the values come from

| Field | Source |
|---|---|
| `host` | `config.yaml` → `platforms.api_server.extra.host` |
| `port` | `config.yaml` → `platforms.api_server.extra.port` |
| `dashboard` | `config.yaml` → `dashboard.public_url` |
| `dash_user` | `config.yaml` → `dashboard.basic_auth.username` |
| `label` | `tailscale status` → `Self.HostName` (or `--label`) |
| `token` | `.env` → `API_SERVER_KEY` (single strong entry) |
| `https` | `tailscale serve status` — an `HTTPS: true` handler whose target port is the chat port |

`tailscale` is consulted because two facts simply are not in the profile config:
whether TLS terminates in front of the chat port, and which hostname is on the
certificate (the node owns several, and the wrong one fails TLS validation).

## What it deliberately does not do

- **Never prints the key.** Not in the state report, not in `--json`, not in the
  QR path. `--link` prints it because a link *is* the credential, and that has to
  be asked for explicitly.
- **Never guesses `https`.** A raw `TCPForward` without `TerminateTLS` is
  reported as undetermined rather than published as a URL the phone cannot reach.
- **Does not claim a verified setup.** The Dashboard password is stored as a
  hash and cannot be recovered; the report says so instead of pretending the app
  will connect fully. Pass one with `--dash-pass-stdin` if you want it embedded.
- **No model tools.** Operator-facing CLI only.

## Rendering

The QR is drawn from `libqrencode` via ctypes (`qrencode` CLI used if present),
so there is no Python dependency to install. Polarity is inverted for dark
terminals, because scanners expect dark modules on a light field. `--ascii`
switches to two-character cells for terminals without half-block glyphs.
