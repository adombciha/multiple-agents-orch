---
name: git-commit
description: Analyze staged Git changes or already-stashed Git changes and propose the most suitable English commit message.
---

# git-commit

## Instructions for Gemini

1. **Identify Changes**:
   - Use `run_command` with `git status --short` to check for staged changes.
   - If there are staged changes, use `git diff --cached --stat` and `git diff --cached` to inspect them.
   - If there are no staged changes, check stashes with `git stash list` and inspect the relevant stash (e.g., `git stash show --stat stash@{0}` and `git diff HEAD stash@{0}`).
2. **Analyze Intent**: Understand the behavior change, fix, or refactor from the diff.
3. **Draft Message**: Generate a concise imperative English commit message. Use conventional commit prefixes if suitable (e.g., `fix:`, `feat:`). Provide an optional body for non-trivial changes.
4. **Format Output**:
   - State the analyzed change set.
   - Output `Recommended commit message:` followed by the message in a fenced text block.
   - Provide a `Why:` section with one or two bullets.
5. **Safety Rule**: DO NOT execute `git commit`, `git push`, `git reset`, `git stash apply/pop/drop`, or any other repository-mutating commands.
