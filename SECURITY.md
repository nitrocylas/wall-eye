# Security policy

Wall-Eye is a local-only application: it has no cloud component, no
accounts, and no telemetry. Its security-relevant surfaces are the optional
ntfy push topic (a bearer secret - see the README's Security notes), the
LAN-facing ESP32 firmware (trust model documented in
firmware/FIRMWARE.md), and the image parsers that handle camera data.

## Reporting a vulnerability

Please report suspected vulnerabilities privately through GitHub's
"Report a vulnerability" feature (Security tab of this repository) rather
than a public issue, so a fix can land before details are public. Include
what you found, where, and how to reproduce it. Reports are appreciated
and will be credited in the fix unless you prefer otherwise.

## Scope notes

- The firmware intentionally has no authentication and trusts the local
  network; reports about that documented design are better raised as
  feature discussions than vulnerabilities.
- Keep opencv-python and Pillow up to date - they parse untrusted camera
  bytes, and their upstream fixes matter more than anything in this repo.
