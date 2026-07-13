# PR AutoFix Agent

A deployable GitHub webhook service that uses your server's authenticated **Claude Code CLI** to:

1. review new or updated pull requests (including from another agent that generates GitHub
   workflows, Terraform, or Kubernetes manifests), make safe repair edits, push them to the same
   branch, and comment with the result;
2. after a merge, watch for failed post-merge pipeline runs, diagnose and fix the root cause on a
   new `autofix/ci-*` branch, and open a repair PR;
3. if that pipeline keeps failing, keep pushing follow-up fix commits to the **same** autofix PR
   (not a new one each time) — up to `MAX_AUTOFIX_ATTEMPTS` — then stop and ask a human to take
   over instead of looping forever.

The service does not merge PRs itself, and it never runs infrastructure-mutating commands (see
guardrails below) — it only edits files and pushes commits for a human to merge.

## How the two flows work

### 1. PR review (`pull_request: opened / synchronize / reopened`)

- Skips repos that aren't allowlisted.
- Fetches the changed files, clones the PR's branch (from the fork if it's a fork), and asks
  Claude Code to inspect the repo and make the smallest safe fix.
- If the working tree changed, commits and pushes back to the same branch. If the branch is on a
  fork without "Allow edits from maintainers," the push will fail — the agent still posts a
  comment explaining that instead of failing silently.
- Posts one PR comment: whether it pushed a fix, plus Claude's own report.
- The agent's own autofix PRs (see below) get exactly **one** such review pass, on creation only —
  otherwise its own fix commit would trigger another review, which pushes again, forever.

### 2. Post-merge pipeline autofix (`workflow_run: completed`, `conclusion: failure`)

- The branch name is derived from the workflow name (`autofix/ci-<workflow-name>`), so repeated
  failures of the *same* workflow always map to the *same* autofix branch/PR.
- **First failure** (triggered by a push, e.g. a normal merge to `main`): clones the base branch,
  creates `autofix/ci-<name>`, asks Claude to diagnose the failure from the run's logs and fix it,
  pushes, and opens a PR labeled `autofix-attempt-1`.
- **Pipeline still failing on that PR** (another `workflow_run` failure for the same branch, from
  either a push or the PR's own checks): pushes another fix commit to the *same* branch/PR and
  bumps the `autofix-attempt-N` label — it does not open a second PR.
- **Attempt cap reached** (`MAX_AUTOFIX_ATTEMPTS`, default 3): stops pushing further fixes, labels
  the PR `autofix-exhausted`, and comments asking a human to take over.
- Per-branch locking prevents two overlapping webhook deliveries for the same branch from racing
  each other or double-counting an attempt.

## Guardrails

- Allowlisted repositories only (`ALLOWED_REPOSITORIES`).
- Signed GitHub webhooks only.
- Claude is explicitly instructed to never run infrastructure-mutating commands (`terraform
  apply`/`destroy`, `kubectl apply`/`delete`, cloud CLI mutations) — only read-only/plan/validate
  commands (`terraform validate`, `terraform plan`, `kubeval`, `kubectl --dry-run`, etc.). It also
  must not touch credentials, secrets, state files, or CI/branch-protection config.
- Capped autofix attempts (`MAX_AUTOFIX_ATTEMPTS`) stop a broken pipeline from being retried
  forever if the root cause isn't something Claude can actually fix (e.g. an outage or missing
  credentials).
- Use a GitHub App token in production. Avoid a personal token with broad access.
- `BOT_LOGIN` must match the actual GitHub login behind `GITHUB_TOKEN` (your PAT's username, or the
  GitHub App's bot slug). This is how the service recognizes its own autofix PRs to review them
  exactly once — if it's wrong, the loop guard silently fails to match and the review→push→
  re-review cycle can run indefinitely.
- The service refuses to start if launched as root — Claude Code itself blocks
  `--dangerously-skip-permissions` under root/sudo for security reasons, so running this as root
  would silently fail on every invocation.

## Server setup

Install Git, GitHub CLI, Python 3.12+, and Claude Code CLI on the server, under a **non-root**
service account. Log in to both CLIs under that same account:

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

The `CLAUDE_COMMAND` account must be logged in with your Claude subscription/API access, under the
same OS user that runs uvicorn — credentials are stored per-user, so logging in as a different
user (e.g. root) won't carry over. The service uses `claude -p` to invoke it non-interactively.

## GitHub configuration

Create a webhook in the target repository:

- Payload URL: `https://YOUR-SERVER/webhooks/github`
- Content type: `application/json`
- Secret: the same `GITHUB_WEBHOOK_SECRET`
- Events: **Pull requests** and **Workflow runs**

For public internet exposure, put the service behind HTTPS (for example Caddy or Nginx). GitHub
requires a reachable HTTPS webhook endpoint.

## Operational notes

`workflow_run` handling only creates a *new* autofix PR for failures triggered by `push` (the
normal post-merge case) — it won't open a PR for a failing check on an unrelated contributor's PR
(that's handled by the PR review flow instead). Before enabling this in a production repository,
add branch protection and review the GitHub App permissions.
