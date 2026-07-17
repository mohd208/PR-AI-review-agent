# PR AutoFix Agent

A deployable service that uses your server's authenticated **Claude Code CLI** to:

1. scan open pull requests (including from another agent that generates GitHub workflows,
   Terraform, or Kubernetes manifests), make safe repair edits, push them to the same branch, and
   comment with the result;
2. also check the CI checks triggered by that same PR — if any of them fail, diagnose the actual
   failure logs and push a fix to the same PR branch, not just review the diff statically;
3. after a merge, watch for failed post-merge pipeline runs, diagnose and fix the root cause on a
   new `autofix/ci-*` branch, and open a repair PR;
4. if a pipeline keeps failing (either on a regular PR or an autofix PR), keep pushing follow-up
   fix commits to the **same** PR (not a new one each time) — up to `MAX_AUTOFIX_ATTEMPTS` — then
   stop and give a human a concrete diagnosis instead of looping forever.

**No GitHub webhook to configure.** The service polls the GitHub API directly with `GITHUB_TOKEN`
every `POLL_INTERVAL_SECONDS` (default 60s) — just point it at your repos in `.env` and run it.

The service does not merge PRs itself, and it never runs infrastructure-mutating commands (see
guardrails below) — it only edits files and pushes commits for a human to merge.

## How it works

Every poll cycle, for each allowlisted repo:

1. **List open PRs.** For every PR that isn't on an `autofix/ci-*` branch, the agent checks two
   independent things and reacts to whichever changed since it last looked:
   - **The diff itself** — if the head commit SHA is new, comment `🔍 Scanning this PR for
     issues...`, clone → ask Claude Code to inspect the repo and make the smallest safe fix →
     commit and push to the same branch (works for forks too; if the push is rejected — no "Allow
     edits from maintainers" — the next comment explains that instead of failing silently). If the
     PR touches `.github/workflows/*`, Claude is also given the actual configured repository
     secret/variable **names** (never secret values — GitHub's API never exposes those) so it can
     flag any `${{ secrets.X }}` / `${{ vars.Y }}` reference that doesn't actually exist, fix an
     obvious typo of an existing name, or leave a genuinely missing one flagged for a human to add.
   - **The PR's own CI checks** — checked every cycle regardless of whether the diff changed, since
     CI often finishes well after the commit that triggered it was already reviewed. If a check run
     for the current head commit fails and hasn't been handled yet, the agent feeds Claude the
     actual failure logs (not just the diff) and pushes a targeted fix to the same branch — capped
     at `MAX_AUTOFIX_ATTEMPTS` the same way the post-merge flow below is, using the same
     `autofix-attempt-N`/`autofix-exhausted` labels and a final read-only diagnosis once exhausted.
   - Either way, one follow-up comment with the outcome: `✅ Everything is good — <reason>` if
     nothing was wrong, or `🔧 Found an issue: <summary> Fixed and pushed.` if it made a change
     (full Claude output collapsed underneath either way). The agent only ever pushes commits to
     the PR's own branch — it never merges, closes, or approves the PR itself.
   - The PR's new head SHA (after our own push, if any) and the last-handled CI run ID are both
     recorded, so the next poll doesn't re-react to our own commit or the same already-seen run —
     this is what replaces webhook loop-prevention entirely.

2. **Check every "watched branch"'s latest completed pipeline run** — not just the repo's
   GitHub-configured default branch. The watch list always includes the default branch, plus every
   branch ever seen as a PR's base (learned from recent PRs of any state, so it still gets learned
   even after that PR is merged/closed) — so teams that merge into `dev`/`develop`/`staging` and
   only promote to `main` separately are covered too, automatically, with no configuration needed.
   - The very first time the agent observes the *default* branch (e.g. on first startup), it just
     records that run as a baseline and does nothing else — so it never "fixes" some pre-existing
     failure that predates the agent watching it.
   - A branch discovered *later* by being seen as a PR's base is treated differently: since
     something relevant clearly just happened (a PR merged into it), the agent reacts immediately
     if it's currently failing, instead of silently baselining it.
   - Either way, whenever a *new* run appears and it failed: derive a branch name from the workflow
     (`autofix/ci-<workflow-name>`), clone that branch, ask Claude to diagnose the failure from the
     run's logs (plus the current secrets/variables inventory, since a missing one is a common root
     cause) and fix it, push to a new branch, open a PR labeled `autofix-attempt-1`.

3. **Check each open `autofix/ci-*` PR's latest completed run:**
   - Still failing → comment `🔍 Pipeline still failing — scanning logs for attempt N/max...`,
     push another fix commit to the *same* PR, bump the `autofix-attempt-N` label.
   - Now passing → comment once that it's ready for review/merge.
   - At `MAX_AUTOFIX_ATTEMPTS` → stop retrying, label `autofix-exhausted`, and run one final
     read-only pass (no edits) asking Claude for a root-cause diagnosis and concrete suggestions
     for a human to act on, included in the "needs a human" comment.

Per-branch locking (in-memory) prevents two overlapping poll cycles from racing each other on the
same branch. State (which SHAs/runs have already been handled) is persisted to `STATE_FILE` so a
restart doesn't cause every open PR to be rescanned at once.

## Guardrails

- Allowlisted repositories only (`ALLOWED_REPOSITORIES`).
- Claude is explicitly instructed to never run infrastructure-mutating commands (`terraform
  apply`/`destroy`, `kubectl apply`/`delete`, cloud CLI mutations) — only read-only/plan/validate
  commands (`terraform validate`, `terraform plan`, `kubeval`, `kubectl --dry-run`, etc.). It also
  must not touch credentials, secrets, state files, or CI/branch-protection config.
- Claude is explicitly instructed to never merge, close, approve, or run any `gh pr` administration
  command — its only capability is editing files in the cloned working tree. This is stated as a
  hard rule in the prompt (not just assumed from what our own code calls), since the server's `gh`
  CLI is authenticated and could otherwise be invoked directly.
- Capped autofix attempts (`MAX_AUTOFIX_ATTEMPTS`) stop a broken pipeline from being retried
  forever if the root cause isn't something Claude can actually fix (e.g. an outage or missing
  credentials).
- `MAX_CONCURRENT_REPAIRS` bounds how many `claude` subprocesses run at once, so a burst of PR
  activity doesn't overload the server.
- Use a GitHub App token in production. Avoid a personal token with broad access.
- Checking secrets/variables referenced in workflows needs `Secrets: Read-only` and
  `Variables: Read-only` on `GITHUB_TOKEN` (in addition to the permissions above). Secret **names**
  are readable this way, never values — GitHub's API doesn't expose secret values to anyone,
  including this agent. Without these permissions, that specific check is skipped (logged, not
  fatal) and everything else keeps working.
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
# edit .env with your token and owner/repository — no webhook secret needed anymore
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The `CLAUDE_COMMAND` account must be logged in with your Claude subscription/API access, under the
same OS user that runs uvicorn — credentials are stored per-user, so logging in as a different
user (e.g. root) won't carry over. The service uses `claude -p` to invoke it non-interactively.

`/healthz` is available for monitoring, but there's no `/webhooks/github` endpoint anymore —
everything is driven by polling.

## Operational notes

Pipeline autofix only creates a *new* PR from a watched branch's own runs; failures on an
unrelated contributor's PR checks are left to the PR-review flow instead. Before enabling this in
a production repository, add branch protection and review the GitHub App/token permissions.

Polling cost scales with the number of allowlisted repos and open PRs — each cycle calls the
GitHub API a handful of times per repo. At the default 60s interval this is well within GitHub's
rate limits for a normal number of repos.
