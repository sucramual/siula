# Working on Siula

Siula is a Python CLI and library for coding agents: it inspects declared delivery contracts and runs a pull request review loop (`siula round`).

## Setup and checks

Use Python 3.12 or newer and [uv](https://docs.astral.sh/uv/). Run these checks before you hand off a change:

```sh
uv sync
uv run pytest -q
uv run ruff check .
uv run ruff format --check src tests
uv run mypy src
```

## Skills

- Skills live in `.agents/skills/<name>/SKILL.md`, the shared location that several agent harnesses read. The frontmatter holds only `name` and `description`, and `name` matches the folder name.
- `.claude/skills` is a relative symlink to `../.agents/skills`, because Claude Code reads nothing under `.agents/`. Claude Code reads this file because the repository has no `CLAUDE.md`; do not add one.
- A skill calls `siula` on `PATH`. Keep core behaviour in the `siula` CLI. A skill uses no harness-specific variable, script path, hook, or plugin.

## Rules

- This repository is public. Never add a private repository's name, a personal path, a person's name, or an employer.
- Write each test first. Run it and see it fail for the expected reason before you change the code.
- A test that expects a refusal asserts the specific error and that nothing after the refusal ran.
- A field that code reads is a closed list. Refuse unknown values.
- Never merge a PR.
