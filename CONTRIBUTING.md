# Contributing

## Development setup

The integration targets Home Assistant 2026.3 or newer, which runs on
Python 3.14. Create a virtual environment with that interpreter and install
the test requirements:

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
```

## Tests

```bash
.venv/bin/python -m pytest
```

`tests/conftest.py` imports the Home Assistant core before any test module;
keep it that way, because HA installs its own validator package under the
`voluptuous` name at import time.

## Continuous integration

`.github/workflows/validate.yml` runs on every pull request: manifest and
translation validation, a Python syntax check and the test suite on
Python 3.14.

## Branches and releases

- `dev` is the working branch; `main` only receives pull requests from `dev`.
- Commit messages follow Conventional Commits. `feat` raises the minor
  version, `fix`/`refactor`/`perf`/`chore` raise the patch version, a `!` or a
  `BREAKING CHANGE:` footer raises the major version; `docs`, `style` and
  `ci` do not change the version.
- Every push to `dev` that touches `custom_components/` or `hacs.json` lets
  `.github/workflows/release.yml` publish a pre-release and commit the new
  version into `manifest.json`. Pull before committing new work, and do not
  edit the version by hand.
- Merging `dev` into `main` publishes the stable release with the base
  version from `manifest.json`.
- Document user-facing changes in `CHANGELOG.md` under `[Unreleased]`. When
  the release pull request is opened, move them into a new
  `## [x.y.z] - YYYY-MM-DD` section; the release workflow does not do this.
