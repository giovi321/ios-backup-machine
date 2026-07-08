# Contributing

Thanks for helping improve iOS Backup Machine. This file is the short version;
the full guide lives in the documentation at
[Contributing](https://giovi321.github.io/ios-backup-machine/development/contributing/)
and [Testing](https://giovi321.github.io/ios-backup-machine/development/testing/).

## Quick start

```bash
git clone https://github.com/giovi321/ios-backup-machine.git
cd ios-backup-machine
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

The test suite is hardware-independent and imports the flat app modules through a
path shim, so it runs anywhere without an e-paper panel.

## Conventions that matter

- Shell scripts and systemd units run on the Linux device, so they must use LF
  line endings, not CRLF.
- `VERSION` in `app/webui.py` is the single source of truth for the version. Bump
  it when you cut a release.
- Update `CHANGELOG.md`, and update `docs/` and `config/config.yaml.example` for
  any new setting.
- Keep pull requests focused on one change.

## Reporting problems

Open an issue for bugs and feature requests. Report security vulnerabilities
privately through
[Security Advisories](https://github.com/giovi321/ios-backup-machine/security/advisories/new),
not as a public issue. See [SECURITY.md](SECURITY.md).
