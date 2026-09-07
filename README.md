# TG Uploader

Cross-platform terminal uploader and text messenger for **Linux, macOS, and Windows** built for Telegram.

> **Independent third-party client.** This project is not affiliated with, sponsored by, or endorsed by Telegram. Every user supplies their own Telegram API credentials and account.

The core client uses Telethon and works without a browser after one-time authorization. When TDLib is available, file uploads use its native C++ transport for higher throughput; otherwise Telethon remains the automatic fallback.

## Features

- Windows, macOS, and Linux terminal support.
- Channels, Groups, Chats, direct `@username`, and Saved Messages.
- Recursive file/folder batches.
- Public Telegram channel media downloads with resume and duplicate protection.
- Optional native TDLib/C++ transfer acceleration.
- Telethon fallback when TDLib is not installed or not ready.
- BLAKE3 content-based duplicate protection per Telegram destination.
- Resume/retry support for interrupted jobs.
- Temporary download files such as `.part` and `.crdownload` are ignored.
- Files still changing during scanning are skipped safely.
- Literal text messaging with a multiline terminal editor.
- Cross-platform single-instance protection for local Telegram session databases.
- Self-contained per-user install; the cloned repository is not required for daily use after installation.

## Platform support

| Platform | Core Telegram CLI | TDLib acceleration |
| --- | --- | --- |
| Linux x86_64 | Yes | Bundled local runtime can be installed automatically during setup |
| Linux other architectures | Yes | Use a compatible system or standard Homebrew TDLib installation |
| macOS | Yes | Standard Homebrew TDLib (`brew install tdlib`) |
| Windows | Yes | Visual Studio vcpkg TDLib installation |

TDLib is optional. If it is unavailable, `telegram login` still finishes successfully with the Telethon transfer engine.

## Requirements

- Python 3.10 or newer.
- A Telegram account.
- Your own Telegram `api_id` and `api_hash` from <https://my.telegram.org>.

Every user must use their **own** Telegram API credentials and account. This repository does not include maintainer credentials, phone numbers, login codes, session files, or account data.

## Install

### Linux / macOS

```bash
chmod +x install.sh
./install.sh
```

You can also run the cross-platform installer directly:

```bash
python3 install.py
```

### Windows PowerShell

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Or:

```powershell
py -3 install.py
```

If the launcher directory is not already on your user `PATH`, the installer adds it for common Bash/Zsh shells or the Windows user environment. Open a new terminal once after that change.

The installer creates an isolated runtime under your per-user application data and installs a normal, non-editable copy of the package. Moving or deleting the cloned repository afterwards does not break the installed command.

Typical application-data locations are:

```text
Linux    ~/.local/share/telegram-uploader
macOS    ~/Library/Application Support/telegram-uploader
Windows  %LOCALAPPDATA%\telegram-uploader
```

## One-time Telegram setup

Run:

```bash
telegram login
```

On a new installation the setup flow is:

```text
TG Uploader setup
✓ API configuration ready
✓ Control session authorized
✓ Fast-transfer session authorized   # when TDLib is available
Setup complete.
```

Your `api_id` is saved in the per-user application-data directory. Your `api_hash` is stored only through an OS-backed credential store when one is available. If secure credential storage is unavailable, the API hash is not written to disk and may be requested again on a later run.

Internally there can be two local Telegram sessions:

1. **Control session (Telethon)** — account access, destinations, text messaging, and transfer fallback.
2. **Fast-transfer session (TDLib)** — optional native file-transfer engine.

On a fresh TDLib setup, Telegram may therefore send one login code for the control session and another for the fast-transfer session. The same API ID/hash and phone number are reused automatically within that setup run. After both sessions are authorized, normal daily use does not ask for Telegram login codes again unless a session is revoked or deleted; the API hash may be requested again only on systems where secure credential storage is unavailable.

If TDLib is not installed on macOS or Windows, setup completes with Telethon. To enable the optional native engine later:

```bash
telegram native setup
telegram login
```

`telegram native setup` uses only standard package-manager locations (Homebrew on macOS/Linux or Visual Studio vcpkg on Windows). Linux x86_64 also supports the project's pinned local TDLib runtime without system installation.

## Commands

```bash
telegram
telegram @username
telegram login
telegram doctor
telegram --version
telegram resume
telegram download @publicchannel
telegram download https://t.me/publicchannel
telegram download resume
```

TDLib diagnostics and benchmarking:

```bash
telegram native doctor
telegram native setup
telegram native login
telegram native bench /path/to/file
```

## Destinations

Running `telegram` offers:

```text
1. Channels
2. Groups
3. Chats
4. Username (@name)
5. Saved Messages
```

Channels, groups, and chats are searchable. A public username can also be passed directly:

```bash
telegram @exampleuser
```

## Send files or text

After selecting a destination:

```text
1. Files / Folders
2. Text
```

### Files / folders

Paste or drag file/folder paths into the terminal, or press `S` to use the native file picker. Directories are scanned recursively.

Successful files are recorded using the Telegram destination plus the BLAKE3 content fingerprint. The same exact bytes are skipped when selected again for the same destination even if the file was renamed or moved. Sending the same file to a different destination remains intentional and allowed.

### Text editor

Choosing `Text` opens a multiline editor when the terminal supports it. On Windows the package installs `windows-curses` for the full-screen editor.

```text
Enter               new line
Arrow keys          move/edit earlier lines
Backspace / Delete  edit normally across lines
Home / End           start/end of current line
Ctrl+U              clear all text
Ctrl+D or F2        send
Esc                 cancel
```

Text is sent literally with Telegram formatting disabled, so underscores, asterisks, URLs, and code-like text are not accidentally reformatted.

## Public channel downloads

Download media from a public Telegram broadcast channel using the same locally authorized Telegram account:

```bash
telegram download @publicchannel
telegram download https://t.me/publicchannel
```

The downloader accepts public channel usernames and `t.me` links only. Private/invite-only links are intentionally rejected. It downloads media through Telegram/Telethon rather than scraping Telegram Web.

Before downloading, TG Uploader scans the channel and shows the message count, media count, and total media size. You then choose **All media/files, Videos, Images, Documents, or Audio**, or choose **Back** without creating a download job. The selected filter is persisted with the resumable job.

Downloads use a bounded four-file concurrent window so batches of small/medium media can use available bandwidth more effectively without opening an unbounded number of Telegram requests. Actual throughput still depends on Telegram, routing, account/server limits, and file sizes.

By default, files are stored under your user Downloads folder in `TG Uploader/<channel name>`. You can choose another local folder when prompted. Remote filenames are sanitized and downloads are written to `.part` files first, then renamed only after completion.

Download duplicate protection is layered:

- The same channel message is not downloaded twice when its completed file still exists.
- Reused Telegram media IDs in another message are skipped without another network download.
- BLAKE3 content fingerprints prevent identical bytes from creating a second local copy even when Telegram exposes them as different media IDs.

Interrupted or partially failed download jobs remain local and can be resumed with:

```bash
telegram download resume
```

Completed media are skipped during resume, so the downloader continues with only work that is still missing. Telegram rate-limit responses are treated as a channel-wide cooldown rather than as hundreds of individual file failures.

## Resume and duplicate protection

```bash
telegram resume
```

Only unfinished Telegram job files are retried. Successfully recorded files remain protected by content-based duplicate detection.

Local upload history is intentionally stored outside the repository. Cloning the code on another computer does not copy previous duplicate-history records.

## Speed

TDLib/C++ is the preferred transfer path when available. On one development account/network, a controlled 256 MB Saved Messages benchmark measured about **11.1 MB/s average**. This is a reference measurement, not a speed guarantee. Actual performance depends on file sizes, connection upload bandwidth, routing, Telegram server/account conditions, and message overhead.

## Privacy and security

Telegram sessions are effectively login credentials. Never commit, publish, upload, or share them.

The repository is source-only. `.gitignore` excludes common local secrets and runtime state including:

- Telegram API configuration files.
- Telethon `.session` files.
- TDLib databases and runtime state.
- local SQLite upload history.
- `.env` files.

The application stores its own configuration/session state in the operating system's per-user application-data directory, not inside the repository. The Telegram API ID may be stored there, but the API hash is persisted only through an OS-backed credential store; if no secure credential store is available, the API hash is not written to disk. Telethon session files remain sensitive login credentials and are restricted to the current user on POSIX systems. New TDLib databases use a random encryption key stored through the same secure credential-store layer; if that secure storage is unavailable, the native TDLib path is not allowed to create an unencrypted database and Telethon remains available as fallback. No application telemetry is implemented.

Native TDLib libraries are loaded only from the application's pinned runtime or standard system/package-manager locations. Environment-variable overrides for arbitrary native libraries are intentionally not accepted.

If a Telegram session is ever exposed, revoke it from Telegram **Settings → Devices** and authorize the tool again.

Do not paste API hashes, login codes, two-step passwords, or session files into public GitHub issues.

## Telegram API responsibility

This is an independent third-party client, not an official Telegram application. Users are responsible for their own Telegram account, API credentials, and compliance with Telegram's API Terms and applicable rules. See <https://core.telegram.org/api/terms>.

## New computer

```bash
git clone <repository-url>
cd telegram-uploader
```

Then install using the command for your operating system and run:

```bash
telegram login
```

Authentication state is intentionally not stored in GitHub, so each new computer requires local authorization.

## License

Released under the MIT License. See [`LICENSE`](LICENSE).

Telegram is a trademark of Telegram Messenger Inc. This independent project is not an official Telegram application.

## Development

Linux/macOS:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
```

Windows PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest
```

GitHub Actions runs the test suite on Ubuntu, macOS, and Windows. The CI tests do not authenticate a Telegram account and never require user credentials.
