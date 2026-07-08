---
title: "Contributing"
description: How to set up the repo, run the tests, follow the repo conventions, and open a pull request against main.
---

Contributions go through a pull request against the `main` branch. This page covers getting the repo running locally, running the test suite before you submit, the few conventions that matter in this codebase (LF line endings, the single version constant, focused changes), and how to open the pull request. The tests are hardware-independent, so you can develop and test on any machine without the Radxa Zero 3W or the e-paper hardware.

## Get set up

Clone the repository:

```bash
git clone https://github.com/giovi321/ios-backup-machine.git
cd ios-backup-machine
```

Install the runtime dependencies (`requirements.txt`) and the test dependencies (`requirements-dev.txt`) together:

```bash
pip install -r requirements.txt -r requirements-dev.txt
```

A virtual environment is optional but keeps these packages isolated from your system Python.

## Run the tests before submitting

Run the suite from the repository root:

```bash
pytest
```

The tests import the flat app modules through a path shim (`tests/conftest.py`), so no e-paper hardware or on-device setup is needed. Everything should pass before you open a pull request. See [Testing](../testing/) for what the suite covers and how it is structured.

## Conventions that matter here

- LF line endings for anything that runs on the device. Shell scripts (`.sh`) and systemd unit files (`.service`) must use LF, not CRLF. A Windows checkout that rewrites these to CRLF will break them on the Linux device
- Single source of version truth. The version lives in one place: the `VERSION` constant in `app/webui.py`. When a change warrants a release, bump that constant and nothing else. Do not add a second version string elsewhere
- Keep changes focused. One logical change per pull request. Smaller, self-contained diffs are easier to review and to roll back

:::caution
If you edit shell scripts or systemd units on Windows, confirm the saved line endings are LF before committing. Configure your editor or `.gitattributes`/`core.autocrlf` so these files are not converted to CRLF.
:::

## Open a pull request

Work happens on `main`: commits land on `main`, and the device's `update.sh` pulls `origin/main`. Open your pull request against `main`.

1. Fork the repository or push a branch, then commit your change with a clear message
2. Run `pytest` and confirm it passes
3. Open a pull request against `main` describing what changed and why
4. Continuous integration runs the suite on every push and pull request (see [Testing](../testing/))

:::tip
Confirm CI is green on your pull request. It runs the same `pytest` you ran locally, across Python 3.11, 3.12, and 3.13.
:::
