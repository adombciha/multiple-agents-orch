---
name: git-commit
description: Analyze staged Git changes or already-stashed Git changes and propose the most suitable English commit message. Use when the user asks for a commit message, says git-commit, wants staged changes summarized, asks to inspect stash contents, compare a stash with the current branch, summarize stashed work, or generate a commit message from changes. This skill must not create commits, apply/pop/drop stashes, push branches, or otherwise mutate repository state unless the user gives a separate explicit instruction outside this skill.
---

# Git Commit

## Core Rule

Do not run `git commit`, `git push`, `git stash apply`, `git stash pop`, `git stash drop`, `git reset`, `git checkout`, `git switch`, `git add`, `git restore`, or any other command that mutates repository state.

Only inspect repository state and diffs. If the user asks for a commit message, provide the message text and stop.

## Workflow

1. Confirm the repository and current branch:
   - Run `git rev-parse --show-toplevel`.
   - Run `git branch --show-current`.
   - Run `git status --short` to notice uncommitted working tree changes without modifying them.

2. Identify the change set to analyze:
   - If `git status --short` shows staged changes, analyze the staged diff by default.
   - Use `git diff --cached --stat`.
   - Use `git diff --cached --name-status`.
   - Use `git diff --cached --find-renames -- <relevant-paths>` when a full patch is needed.
   - If there are staged changes and the user did not ask about a stash, do not inspect stash contents.
   - If there are no staged changes, or the user explicitly asks about a stash, continue with the stash workflow below.

3. Identify the stash to analyze when needed:
   - Run `git stash list`.
   - If the user names a stash, use that stash ref, such as `stash@{2}`.
   - If the user does not name a stash, use `stash@{0}`.
   - If there is no stash, report that there is no stashed work to analyze.

4. Inspect the stash safely:
   - Run `git stash show --stat <stash-ref>`.
   - Run `git stash show --name-status <stash-ref>`.
   - Run `git diff --stat HEAD <stash-ref>`.
   - Run `git diff --name-status HEAD <stash-ref>`.
   - Run `git diff --find-renames HEAD <stash-ref> -- <relevant-paths>` when a full patch is needed.

5. Compare against the current branch:
   - Treat `HEAD` on the current branch as the base.
   - Prefer `git diff HEAD <stash-ref>` over applying the stash.
   - Include staged, unstaged, and untracked stashed files when visible through the stash ref.
   - If untracked stash contents are not visible in the normal diff, inspect stash parents with `git show --stat <stash-ref>^3` and `git diff <stash-ref>^3^ <stash-ref>^3` when that parent exists.

6. Analyze intent before wording:
   - Identify the main behavior change, fix, refactor, test update, documentation change, or tooling change.
   - Group related files by feature area.
   - Ignore generated files, formatting churn, lockfile-only noise, or unrelated local changes unless they materially affect the commit.
   - Prefer one cohesive commit message. If the stash clearly contains unrelated changes, say so and propose separate commit messages.

## Commit Message Style

Return English commit messages only. Prefer concise imperative mood:

```text
Add qPCR calibration export validation
```

Use a conventional commit prefix only when it clearly fits the repository style or the user asks for it:

```text
fix: handle missing qPCR calibration images
test: cover stashed run analyzer edge cases
```

For non-trivial changes, include an optional body:

```text
Fix qPCR calibration image fallback

Use the stored well image when the run output is missing a generated
calibration frame, and cover the fallback path in unit tests.
```

## Response Format

Keep the response short and directly usable:

- State the analyzed change set and current branch, such as staged changes on `feature-x` or `stash@{0}` on `feature-x`.
- Provide `Recommended commit message:` followed by the message in a fenced text block.
- Add `Why:` with one or two bullets summarizing the evidence from the diff.
- Add `Split suggestion:` only when the stash contains unrelated work.
- Do not include commands to commit or push unless the user explicitly asks for commands, and even then do not execute them.
