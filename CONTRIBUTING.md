# Contributing

Thanks for helping make Video Evidence Workbench more useful and trustworthy.

## Before opening a change

1. Search existing issues for the same problem.
2. Keep the change focused on one observable outcome.
3. For a bug, include a minimal reproduction using non-sensitive media or the synthetic smoke fixture.
4. For a data-contract change, describe migration and backward-compatibility impact.
5. Do not commit source videos, provider keys, private URLs, passwords, or generated workspaces.

The project is pre-1.0. Proposals that simplify the evidence workflow are preferred over new platform surfaces such as accounts, payments, or hosted collaboration.

## Local setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
npm --prefix frontend ci

PYTHONPATH=src .venv/bin/python -m video_analysis_mvp.cli doctor
```

## Required checks

Run checks that cover the changed surface:

```bash
PYTHONPATH=src python3 -m compileall -q src tests
PYTHONPATH=src python3 -m unittest discover -v
sh scripts/smoke-test.sh
sh scripts/demo-smoke-test.sh
sh scripts/api-smoke-test.sh
sh scripts/install-smoke-test.sh
npm --prefix frontend run test:integration
npm --prefix frontend run build
npm --prefix frontend audit --audit-level=high
ruff check src tests
bandit -q -r src -ll
pip-audit --local --skip-editable --progress-spinner off
```

The audit commands use the same pinned tools as CI: `ruff==0.15.22`, `bandit==1.9.4`, and `pip-audit==2.10.1`. If a check cannot run, say why in the pull request. Do not report it as passed.

## Change expectations

- Preserve the local-first default and disclose every external network boundary.
- Keep deterministic extraction separate from model-generated annotations.
- Keep timecodes, source references, confidence, readiness, and lineage inspectable.
- Avoid provider-specific assumptions in core schemas.
- Match existing Python and TypeScript style; do not add a dependency for a small utility.
- Update README or docs when a command, artifact, schema, privacy boundary, or UI behavior changes.
- Add the smallest useful regression check for a bug fix.

## Pull requests

A useful pull request explains:

- the user-visible problem;
- the chosen scope and what is deliberately excluded;
- verification commands and results;
- screenshots for desktop and mobile UI changes;
- data migration, provider, privacy, or performance risks.

By contributing, you agree that your contribution is licensed under the repository's MIT License and follows [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
