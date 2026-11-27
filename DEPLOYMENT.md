# TapIn deployment checklist

Deploy the reviewed branch to staging first. Keep existing production data backed up. Render persistent disks require a compatible paid service; do not purchase without owner approval. In Render attach a disk at `/var/data` and set `TAPIN_DATA_DIR=/var/data/tapin`; ensure `TAPIN_DB` is unset or points inside that directory. Start with 15 GB for the current maximum of 40 clips × 250 MiB plus database and overhead; adjust after usage measurement. Keep one instance/worker for SQLite and in-process concurrency controls. Verify backup and restore before moving production data; a disk alone is not a backup. Never commit secrets.

Environment variables:
- `OPENAI_API_KEY`: existing server-only key.
- `TAPIN_DATA_DIR`: `/var/data/tapin`.
- `TAPIN_COOKIE_SECURE`: `1` for HTTPS.
- `TAPIN_OWNER_USER_ID`: sign up as the owner first, then copy the account ID displayed in the authenticated profile into this environment variable. Never use a public username as an authorization credential. Confirm owner dashboard is forbidden to a second account.
- `TAPIN_STREAMS_JSON`: JSON array of real owner-approved channel URLs, e.g. `[{"name":"Channel name","url":"https://www.twitch.tv/REPLACE_ME"}]`. Example is a placeholder; do not configure it. Links are not verified live status or native broadcasting.
- `TAPIN_DONATION_URL` and `TAPIN_DONATION_RECIPIENT`: verified owner-approved payment page and recipient display name. Do not use a guessed payment address. No card processing in TapIn.
- `TAPIN_DAILY_REVIEWS`, `TAPIN_GLOBAL_DAILY_REVIEWS`, `TAPIN_MAX_TOTAL_CLIPS`: tune budget/storage limits; defaults 5, 30, 40.

Release checks: run `python -m compileall server.py`; test signup, upload, analyze, reload and redeploy persistence; cross-account video and admin denial; post/report/hide/ban chat in two browsers; check streaming links against actual channel; verify donation recipient independently; check visitor counts with privacy opt-out and note browser estimates are not people; test emoji drawing/layers/export on Android and desktop. Review chat moderation and privacy policies before public access. Native streaming and payment processing remain unimplemented. Emoji projects are session-local and export PNG only. No actual Render settings changed by this commit.
