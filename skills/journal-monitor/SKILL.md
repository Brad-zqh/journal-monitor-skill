---
name: journal-monitor
description: Set up and maintain journal-submission tracking for Editorial Manager and ScholarOne, including journal configuration, GitHub Actions schedules, status parsing, and email or ServerChan notifications. Use for an author's submission status, not literature discovery.
---

# Journal Monitor

Use the Journal Monitor Python tool to track the user's submissions, configure journals and credentials, and diagnose scheduled checks.

## Locate the runtime

Find the user's actual runtime checkout and verify its Git remote before editing. Read `README.md`, `journals_config.json`, `.github/workflows/check.yml`, and the relevant functions in `check_journals.py`. Treat the configured runtime as authoritative; do not replace it with an older loose copy.

The reusable source template is `https://github.com/Brad-zqh/journal-monitor-skill`. When the user requests a new GitHub-hosted monitor, create a private runtime from the template using their authorized account. Installing this skill alone does not authorize repository creation, secret changes, notifications, or scheduled runs.

Use the user's storage location. Keep maintenance scripts and browser data outside tracked files.

## Configure the smallest change

- **Add a journal:** verify its official submission URL and platform; edit `journals_config.json`. Connect `username_env` / `password_env` to environment variables or GitHub Secrets. For a new Secret name, add its environment mapping to the workflow. Credentials in an existing, explicitly authorized source can be reused; do not print their values.
- **Activate a journal:** check both `enabled` and the optional repository Variable `JOURNALS_ENABLED`, which overrides per-journal enabled flags. In the supplied private-runtime workflow, `JOURNAL_MONITOR_ENABLED=true` is also required.
- **Hide a manuscript:** add its ID or `JOURNAL::ID` to `ignore_ids`. Preserve the history the user wants to retain. Remove an old active-state override when a resubmission replaces the old record.
- **Correct a status:** use `status_overrides` only for a user-confirmed correction, with a source and date. Do not freeze a newly submitted manuscript's state when normal scraping should update it.
- **Change frequency:** edit the cron and matching documentation. GitHub cron is UTC; the default is `17 0,12 * * *` for Beijing 08:17 / 20:17. Preserve manual dispatch and allow for scheduler delays.
- **Parse a changed page:** reproduce from current, redacted HTML or screenshot evidence, make a narrow fix, and add a focused regression test with synthetic data.

Keep real credentials in process environment, GitHub Secrets, or ignored `journals_private.json`. Do not retrieve unrelated account credentials. For login failures, inspect the actual server response and stop before exhausting the site's lockout limit.

## Verify login and delivery

Validate a new account with an authenticated, notification-free check using `JOURNAL_DISABLE_NOTIFICATIONS=1`. Verify the expected manuscript rather than treating navigation or a printed login message as proof of authentication. Preserve unrelated journals' caches when checking a subset.

Treat test notification delivery as a separate external action requiring the user's request. For authorized notification diagnosis, inspect explicit channel success or exhausted-retry failures. A green workflow or successful dispatch alone does not prove delivery. Email and ServerChan should be attempted independently.

The public template's scheduled live job is private-repository-only. Keep `.journal-monitor/`, legacy `article_status_cache.json`, decision letters, screenshots, browser profiles, private JSON, and `.env` out of public Git history. The workflow uses Actions Cache rather than committing manuscript records. Cache eviction can reset the baseline.

## Validate and publish

Before committing, inspect the complete diff and explicit file list. Run:

```text
python <this-skill-directory>/scripts/audit_repo.py <runtime-repository>
python -m unittest discover -v
python -m py_compile check_journals.py
```

Run the last two commands from the runtime repository. The audit is read-only and checks structure, tracked private files, and public credential fields; it is not a complete secret scanner. Resolve errors, stage explicit paths, and publish within the user's authorized scope. Verify the remote commit and effective journal whitelist afterward. Do not change unrelated Secrets.
