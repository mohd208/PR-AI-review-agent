# PR AutoFix Agent

A deployable GitHub webhook service that uses your server's authenticated **Claude Code CLI** to:

1. inspect new or updated pull requests, make safe repair edits, push them to the same PR branch, and comment with its result;
2. inspect failed workflow runs after a merge, create an `autofix/ci-run-*` branch, and open a repair PR.

It keeps operating as GitHub sends new PR and CI events. The service does not merge PRs itself.

## Important guardrails

- Allowlisted repositories only (`ALLOWED_REPOSITORIES`).
- Signed GitHub webhooks only.
- Reviews its own autofix PRs exactly once, on creation, so a review's own push can't trigger another review (no infinite loop).
- Gives Claude a narrow repair task and requires small, safe changes; commits happen only when the working tree changed.
- Fork PRs are reviewed too; if the fix can't be pushed back to the fork (no "Allow edits from maintainers"), the PR comment explains why instead of failing silently.
- Use a GitHub App token in production. Avoid a personal token with broad access.

## Server setup

Install Git, GitHub CLI, Python 3.12+, and Claude Code CLI on the server. Log in to both CLIs under the service account:

```bash
claude
gh auth login
```

Then:

```bash
cd pr-autofix-agent
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env with your token, webhook secret, and owner/repository
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The `CLAUDE_COMMAND` account must be logged in with your Claude Pro subscription. The service uses `claude -p` to invoke it non-interactively.

## GitHub configuration

Create a webhook in the target repository:

- Payload URL: `https://YOUR-SERVER/webhooks/github`
- Content type: `application/json`
- Secret: the same `GITHUB_WEBHOOK_SECRET`
- Events: **Pull requests** and **Workflow runs**

For public internet exposure, put the service behind HTTPS (for example Caddy or Nginx). GitHub requires a reachable HTTPS webhook endpoint.

## Operational notes

`workflow_run` repair is intentionally limited to failed workflows triggered by `push`—the normal post-merge case. It does not repair untrusted fork PRs. Before enabling this in a production repository, add branch protection and review the GitHub App permissions.

The agent uses `--dangerously-skip-permissions` because it must edit a temporary clone non-interactively. Run it in an isolated, least-privileged server/container and only with the repository allowlist above.
