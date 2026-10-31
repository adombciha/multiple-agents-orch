"""Format fully staged source files, leaving index updates to the user."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

PRETTIER_SUFFIXES = {
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".css",
    ".html",
}


def git_paths(*args):
    output = subprocess.check_output(["git", "diff", "--name-only", "-z", *args])
    return [Path(os.fsdecode(path)) for path in output.split(b"\0") if path]


def main():
    root = Path(
        subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], text=True
        ).strip()
    )
    os.chdir(root)
    files = [
        path
        for path in git_paths("--cached", "--diff-filter=ACMR")
        if path.suffix.lower() in PRETTIER_SUFFIXES | {".py", ".pyi"}
        and not path.is_symlink()
    ]
    if not files:
        return 0
    partial = set(files) & set(git_paths())
    if partial:
        print(
            "Formatter stopped: these staged files also have unstaged changes. Format and stage them explicitly:",
            file=sys.stderr,
        )
        for path in sorted(partial):
            print(f"  {path}", file=sys.stderr)
        return 1
    for path in files:
        if not path.is_file() or not path.resolve().is_relative_to(root):
            print(
                f"Formatter stopped: invalid working-tree path: {path}", file=sys.stderr
            )
            return 1
    groups = {
        "pyink": [path for path in files if path.suffix.lower() in {".py", ".pyi"}],
        "prettier": [
            path for path in files if path.suffix.lower() in PRETTIER_SUFFIXES
        ],
    }
    commands = {}
    for tool, paths in groups.items():
        if not paths:
            continue
        candidates = (
            [root / ".venv" / "bin" / tool]
            if tool == "pyink"
            else [root / "node_modules" / ".bin" / tool]
        )
        executable = next(
            (str(path) for path in candidates if os.access(path, os.X_OK)), None
        ) or shutil.which(tool)
        if not executable:
            print(
                f"Formatter stopped: {tool} is missing. See docs/formatting.md.",
                file=sys.stderr,
            )
            return 1
        commands[tool] = [
            executable,
            *(
                ["--write", "--ignore-unknown"]
                if tool == "prettier"
                else ["--workers", "1"]
            ),
            *[f"./{path}" for path in paths],
        ]
    for command in commands.values():
        result = subprocess.run(command, check=False)
        if result.returncode:
            print(
                "Formatter failed. Review working-tree changes before retrying; the index was not updated.",
                file=sys.stderr,
            )
            return 1
    if set(files) & set(git_paths()):
        print(
            "Files were formatted. Review the diff, stage the formatted files, then retry the commit.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Formatter failed: {error}", file=sys.stderr)
        raise SystemExit(1)
