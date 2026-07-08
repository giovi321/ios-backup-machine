# Security policy

## Reporting a vulnerability

Report security issues privately through
[GitHub Security Advisories](https://github.com/giovi321/ios-backup-machine/security/advisories/new).
Do not open a public issue for a vulnerability.

Please include what you found, how to reproduce it, and the version or commit
you tested. You will get an acknowledgement, and a fix or a decision, as soon as
is practical for a single-maintainer project.

## Supported versions

Fixes land on the latest release. Update to the newest version before reporting,
in case the issue is already resolved.

## Scope and intended use

This is a personal appliance meant to run offline or on a trusted network. It is
not hardened for direct exposure to the public internet:

- The web UI password is optional and basic, with no rate limiting or lockout.
  Keep the device on your LAN or reach it over the VPN, and bind the web UI to
  the interfaces you trust.
- The iOS backup payload is encrypted by the iPhone with a password that never
  leaves the phone. WireGuard and remote sync credentials are encrypted at rest
  with AES-256-GCM.

See [Security](https://giovi321.github.io/ios-backup-machine/architecture/security/)
in the documentation for the full picture.
