# Securing the ScanPi web UI

Three deployment modes, ranked by trust boundary:

## 1. LAN-only (default, lowest risk)

Out of the box ScanPi binds `0.0.0.0:8080` and has no auth. This is fine
because:

- Anyone on your home Wi-Fi can already see your traffic
- The UI doesn't accept actions that affect the outside world (it can't TX)
- Watchlist edits + transcription target switches are LAN-side only

If you trust everyone on your LAN, you're done.

## 2. Tailscale-only (recommended)

```bash
sudo tailscale up
sudo tailscale serve --bg --https=443 http://localhost:8080
```

The web UI is now reachable at `https://scanpi.<your-tailnet>.ts.net` from
*any device on your tailnet*, with a real Let's Encrypt cert. Anyone not
on your tailnet gets DNS NXDOMAIN.

This is auth at the network layer. Tailscale's identity model handles login,
device approval, ACLs, and key rotation. **You do not need application-layer
auth in this mode** — your tailnet membership IS the auth.

To restrict further (e.g., only let your phone reach ScanPi, not your wife's
laptop), use Tailscale ACLs:

```hujson
{
  "acls": [
    {
      "action": "accept",
      "src": ["patrick@example.com"],
      "dst": ["scanpi:443"]
    }
  ]
}
```

## 3. Public via Tailscale Funnel (highest risk — requires app auth)

```bash
sudo tailscale funnel --bg --https=443 http://localhost:8080
```

ScanPi is now reachable from the public internet at the same `.ts.net` URL.
Anyone who guesses or scrapes the hostname can hit it. **Do not enable this
without app-layer auth in front.**

Two recommended app-layer layers:

### a) Caddy + basic auth (5 min, sufficient for "share with one friend")

```bash
sudo apt install caddy
```

Edit `/etc/caddy/Caddyfile`:

```
:8443 {
  basicauth {
    yourusername $2a$14$...   # generate via: caddy hash-password
  }
  reverse_proxy localhost:8080
}
```

Then:

```bash
sudo systemctl restart caddy
sudo tailscale funnel --bg --https=443 http://localhost:8443
```

### b) Authelia / Authentik (for SSO + TOTP)

Heavyweight, but if you're already running an Authelia for the rest of your
homelab, just add ScanPi as a protected upstream. Out of scope here — see
the Authelia docs for proxy_pass examples.

### c) WireGuard split-tunnel for non-tailnet users

If a friend isn't on your tailnet but you want to share occasionally, give
them a WireGuard config (or a tailnet "tagged" key) instead of opening Funnel.

## Funnel rate limits

Tailscale Funnel is rate-limited (~1k req/day for free, ~10k for paid).
Fine for personal use. **Don't use Funnel to broadcast a scanner stream
to the public** — push audio to Broadcastify or run an Icecast on a real
VPS instead.

## Hardening checklist

- [ ] Tailnet ACL restricts who can reach the Pi
- [ ] `mosquitto` listener is `localhost` (lite default) or behind a password
      (full default has it open on all interfaces — restrict if exposed)
- [ ] Watchlist + alerts.db not exposed to Funnel without auth
- [ ] No real names in transcripts shared with non-trusted parties
- [ ] OS up to date: `sudo apt upgrade -y` weekly
- [ ] SSH disabled or key-only (`PasswordAuthentication no` in sshd_config)
