#!/usr/bin/env python3
"""Read-only structural and secret-safety audit for journal-monitor repositories."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


REQUIRED_FILES = (
    "check_journals.py",
    "journals_config.json",
    "journals_private.example.json",
    "requirements.txt",
    ".github/workflows/check.yml",
)
SENSITIVE_TRACKED_NAMES = {
    ".env",
    "journals_private.json",
    "article_status_cache.json",
}
SENSITIVE_SUFFIXES = (".log", ".pem", ".key", ".png", ".jpg", ".jpeg")
VALID_PLATFORMS = {"editorialmanager", "scholarone"}
CRON_RE = re.compile(r"^\s*-\s*cron:\s*[\"']([^\"']+)[\"']\s*$", re.MULTILINE)


def git_tracked_files(repo: Path) -> set[str]:
    """Return repository-relative tracked paths without reading their contents."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return {
        item.decode("utf-8").replace("\\", "/")
        for item in result.stdout.split(b"\0")
        if item
    }


def audit(repo: Path) -> tuple[list[str], list[str], list[str]]:
    """Return errors, warnings, and informational messages for a repository."""
    errors: list[str] = []
    warnings: list[str] = []
    info: list[str] = []

    for relative in REQUIRED_FILES:
        if not (repo / relative).is_file():
            errors.append(f"missing required file: {relative}")

    if errors:
        return errors, warnings, info

    try:
        config = json.loads((repo / "journals_config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"invalid journals_config.json: {exc}")
        return errors, warnings, info

    journals = config.get("journals")
    if not isinstance(journals, list) or not journals:
        errors.append("journals_config.json must contain a non-empty journals list")
    else:
        names: list[str] = []
        enabled: list[str] = []
        for index, journal in enumerate(journals):
            if not isinstance(journal, dict):
                errors.append(f"journals[{index}] must be an object")
                continue
            if journal.get('username') or journal.get('password'):
                errors.append(f'journals[{index}] contains a literal credential; use *_env or ignored private JSON')
            missing = [key for key in ("name", "full", "enabled", "platform", "url") if key not in journal]
            if missing:
                errors.append(f"journals[{index}] missing fields: {', '.join(missing)}")
            name = str(journal.get("name", "")).strip()
            if name:
                names.append(name)
                if journal.get("enabled") is True:
                    enabled.append(name)
            platform = str(journal.get("platform", "")).lower()
            if platform and platform not in VALID_PLATFORMS:
                errors.append(f"journal {name or index} has unsupported platform: {platform}")
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            errors.append(f"duplicate journal names: {', '.join(duplicates)}")
        info.append(f"configured journals: {len(names)}; enabled: {', '.join(enabled) or 'none'}")

    for key in ('username', 'password', 'email_sender', 'email_password', 'email_receiver', 'serverchan_key'):
        if config.get(key):
            errors.append(f'public configuration contains a private field value: {key}')

    workflow_text = (repo / ".github/workflows/check.yml").read_text(encoding="utf-8")
    crons = CRON_RE.findall(workflow_text)
    if not crons:
        errors.append("check.yml has no quoted cron schedule")
    else:
        info.append(f"workflow cron (UTC): {', '.join(crons)}")
    if "workflow_dispatch:" not in workflow_text:
        warnings.append("check.yml has no workflow_dispatch trigger")

    try:
        tracked = git_tracked_files(repo)
    except (OSError, subprocess.CalledProcessError) as exc:
        errors.append(f"unable to inspect tracked files: {exc}")
        return errors, warnings, info

    unsafe = sorted(
        path
        for path in tracked
        if Path(path).name in SENSITIVE_TRACKED_NAMES
        or Path(path).suffix.lower() in SENSITIVE_SUFFIXES
        or path.startswith(('.journal-monitor/', 'browser-data/'))
        or (Path(path).name.startswith('.env.') and Path(path).name != '.env.example')
    )
    if unsafe:
        errors.append(f"sensitive file names are tracked: {', '.join(unsafe)}")

    ignore_path = repo / ".gitignore"
    ignore_text = ignore_path.read_text(encoding="utf-8") if ignore_path.is_file() else ""
    for pattern in ("journals_private.json", ".env", "*.log", "*.png"):
        if pattern not in ignore_text.splitlines():
            warnings.append(f".gitignore does not explicitly include: {pattern}")

    return errors, warnings, info


def main() -> int:
    """Run the audit and return a process-friendly status code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", nargs="?", default=".", type=Path)
    args = parser.parse_args()
    repo = args.repo.expanduser().resolve()

    errors, warnings, info = audit(repo)
    for message in info:
        print(f"INFO: {message}")
    for message in warnings:
        print(f"WARNING: {message}")
    for message in errors:
        print(f"ERROR: {message}")
    print(f"SUMMARY: {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
