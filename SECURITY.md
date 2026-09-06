# Security Policy

## Protect Telegram credentials

Telegram API hashes, login codes, two-step passwords, Telethon session files, and TDLib databases must be treated as sensitive account credentials.

Do not include them in commits, releases, screenshots, logs, or public GitHub issues. The project stores normal authentication state outside the repository in the operating system's per-user application-data directory.

If a Telegram session may have been exposed, revoke that session from Telegram **Settings → Devices** and authorize the tool again.

## Reporting a vulnerability

Please avoid posting exploit details or credentials in a public issue. Use GitHub's private security-advisory reporting for the repository when available, and include only the minimum reproduction information needed to investigate the problem.

Never attach real `.session` files, TDLib database directories, `telegram-api.json`, `.env` files, login codes, or two-step passwords to a report.

## Scope

Security reports about credential exposure, unsafe local-file handling, command execution, dependency loading, session isolation, and unintended Telegram message/file actions are especially useful.
