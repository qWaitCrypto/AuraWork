# Contributing to AuraWork

## Development Setup

```bash
git clone https://github.com/qWaitCrypto/AuraWork.git
cd AuraWork
python -m venv .venv
source .venv/bin/activate
pip install -e ".[office,web]"
```

## Running Tests

```bash
pytest tests/
```

No API keys or external services are required for the test suite.

## Commit Conventions

We use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(runtime): add stateless DAG scheduler
fix(web): correct approval routing on reconnect
docs: update README architecture diagram
chore: clean up build artifacts
refactor: simplify parallel dispatch schema validation
```

Common scopes: `runtime`, `web`, `cli`, `events`, `tools`.

## Pull Requests

1. Create a feature branch from `main`
2. Make your changes with clear, focused commits
3. Ensure `pytest tests/` passes
4. Open a PR with a brief description of what and why
