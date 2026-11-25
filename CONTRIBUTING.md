# Contributing

## Setup

You need Python 3.14 or later and [uv](https://docs.astral.sh/uv/). `uv sync` installs the app along with the dev tools.

## Checks

CI runs these checks on the oldest Python that `requires-python` in `pyproject.toml` allows, currently 3.14. The tests use fakes for the MOTU and the panel, so they need neither device nor a config file.

```bash
UV_PYTHON=3.14 uv run ruff check .
UV_PYTHON=3.14 uv run ruff format --check .
UV_PYTHON=3.14 uv run pytest
```

## Pull requests

Keep each pull request focused on one change, and open separate pull requests for unrelated changes. Pull requests are squash-merged, so the title becomes the commit subject on master and the description becomes the commit body.

The title follows [Conventional Commits](https://www.conventionalcommits.org/), and CI checks it:

- The type is lowercase: `feat`, `fix`, `perf`, `refactor`, `test`, `ci`, `docs`, `build`, `style`, `chore` or `revert`
- The description is lowercase, as in `feat: add fader hold` or `fix: keep solo override on reconnect`
- A `!` after the type marks a breaking change, as in `refactor!: rename the level API`. A `BREAKING CHANGE:` footer in the description does the same.

A pull request with a single change keeps a plain description. When a pull request contains several changes, list each one as its own conventional entry in the description, separated by blank lines, so each gets a changelog entry:

```text
feat: add ring presets

fix: keep solo override on reconnect
```

The maintainer may edit the title and description before merging.

Pull requests don't change the version. Releases are handled by [release-please](https://github.com/googleapis/release-please), which bumps the version and writes [CHANGELOG.md](CHANGELOG.md) from the merged titles and descriptions.
