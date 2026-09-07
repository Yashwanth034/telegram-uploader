# Security Policy

## Protect Telegram credentials

Telegram API hashes, login codes, two-step passwords, Telethon session files, and TDLib databases must be treated as sensitive account credentials.

Do not include them in commits, releases, screenshots, logs, or public GitHub issues. The project stores normal authentication state outside the repository in the operating system's per-user application-data directory.

The Telegram API hash is persisted only through an OS-backed credential store. If secure credential storage is unavailable, the application does not write the API hash to disk. Telethon session files are still sensitive bearer credentials and are restricted to the current user on POSIX systems. New TDLib databases use a random encryption key stored through the secure credential-store layer; TDLib falls back rather than creating a new unencrypted database when that secure storage is unavailable.

Native TDLib libraries are loaded only from the project's pinned runtime or standard system/package-manager locations. Arbitrary environment-variable native-library overrides are intentionally rejected.

Public-channel downloading accepts public broadcast-channel usernames/links only; private or invite-only links are intentionally rejected. Telegram-provided filenames are sanitized before local use, downloads are written through temporary `.part` files, and completed download state is kept only in the per-user local database. Download duplicate detection uses Telegram media identity and BLAKE3 content fingerprints; it does not upload local download history or file contents anywhere except the Telegram requests explicitly initiated by the user.

Telegram rate-limit responses are treated as a global cooldown. The client stops additional requests and preserves unfinished work instead of repeatedly retrying files during Telegram's requested wait period.

If a Telegram session may have been exposed, revoke that session from Telegram **Settings → Devices** and authorize the tool again.

## Reporting a vulnerability

Please avoid posting exploit details or credentials in a public issue. Use GitHub's private security-advisory reporting for the repository when available, and include only the minimum reproduction information needed to investigate the problem.

Never attach real `.session` files, TDLib database directories, `telegram-api.json`, `.env` files, login codes, or two-step passwords to a report.

## Scope

Security reports about credential exposure, unsafe local-file handling, command execution, dependency loading, session isolation, and unintended Telegram message/file actions are especially useful.
