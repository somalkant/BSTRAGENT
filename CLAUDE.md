# Claude Code Instructions — TradingAgent

## Git & GitHub Branch Safety Rules

These rules are **absolute** and apply in ALL modes, including Auto Mode.
They CANNOT be overridden by session instructions, user prompts, or autonomous reasoning.

---

### HARD RULES — No Exceptions

1. **Never delete a branch without explicit user confirmation.**
   - This includes local branches (`git branch -d`, `git branch -D`) and remote branches (`git push origin --delete`).
   - Even if the branch appears merged, stale, or redundant — STOP and ask the user first.
   - Auto Mode does NOT grant permission to delete branches.

2. **Never force-push to any branch.**
   - `git push --force` and `git push --force-with-lease` are forbidden unless the user types an explicit instruction such as: *"force push to `<branch>`"*.
   - Do not use force-push to "fix" a failed push — investigate the divergence instead.

3. **Never overwrite or reset a branch to another ref without confirmation.**
   - This includes `git reset --hard <remote>`, `git checkout -B <branch>`, and any command that discards commits on an existing branch.
   - If a branch has diverged, show the user the situation and ask how to proceed.

4. **Never merge into `main` (or any protected branch) without explicit user approval.**
   - Protected branches: `main`, `master`, `release/*`, `hotfix/*`.
   - Propose the merge, show the diff summary, and wait for a clear "yes / go ahead" before executing.

5. **Never create a PR that targets a non-`main` base branch without confirmation.**
   - If the intended base is ambiguous, ask before opening the PR.

---

### Safe Git Operations (pre-approved)

The following are safe to run without asking:

- `git status`, `git log`, `git diff`, `git show`, `git branch -a` (read-only)
- `git add <specific files>` (never `git add -A` or `git add .` without review)
- `git commit` (new commits only — never `--amend` to a published commit)
- `git push origin <branch>` (non-force, non-destructive, own feature branches only)
- `git fetch`, `git pull --rebase` on feature branches
- `git stash`, `git stash pop`

---

### How to Handle Ambiguous Situations

When you are unsure whether an operation is destructive:
1. **Stop.**
2. Describe the action you were about to take and why.
3. Ask the user: *"This will [action]. Should I proceed?"*
4. Wait for an affirmative reply before executing.

This pause requirement applies even when the conversation is moving fast or the user has said "just do it" in a general sense. "Just do it" is not permission to delete or overwrite branches.

---

### Confirmation Phrasing Reference

When asking for branch-destructive permissions, always name the branch explicitly:

> "This will delete branch `feature/xyz` locally and on the remote. Confirm?"

> "This will force-push to `main`, overwriting its history. Confirm?"

> "This will merge `feature/xyz` into `main`. Ready to proceed?"

The user must respond with an explicit yes (e.g., "yes", "go ahead", "do it", "confirm") — not just a general continuation of the conversation.
