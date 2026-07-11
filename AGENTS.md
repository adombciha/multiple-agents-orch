# Orchestrator Agent Contract

Follow the workflow stage stated in the prompt.

## Planning, review, QA, and research

- Return only the format requested by the prompt.
- Do not emit file-marker blocks unless the prompt explicitly says `IMPLEMENTING`.
- Do not edit files directly unless the prompt explicitly authorizes it.

## IMPLEMENTING

- Modify only files needed for the assigned task, using paths relative to the repository root.
- Your entire response must consist only of one or more blocks in this exact format:

```text
[FILE_START: relative/path.ext]
file contents
[FILE_END: relative/path.ext]
```

- The first non-empty character must be `[` from a `FILE_START` block.
- Do not output analysis, recommendations, summaries, Markdown fences, diffs, or prose.
- Do not emit a task that only gathers information, creates a plan, reviews, or verifies; those are not implementation tasks.
