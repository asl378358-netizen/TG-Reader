# TG Reader

Windows desktop reader for the owner's selected Telegram groups.

- Keep `tdata`, Telegram sessions, API credentials, messages and logs out of Git.
- Preserve `%LOCALAPPDATA%\TelegramDailyReader` user data during upgrades.
- Use existing CPython 3.10–3.14 x64; do not download another Python.
- Do not open a browser or request new GitHub login from the updater.
- Read `README.md` before changing installation or release behavior.
- Run `python -m unittest discover -s tests -p "test_*.py"` for application changes.
- Run `tests/test_installer.ps1` and `tests/test_bootstrap.ps1` for Windows installer changes.
- Run `python tools/build_setup.py` before committing: it regenerates the manifest and one-file setup.
- `main` contains development; CI promotes a passing commit to `stable`. Clients update from `stable`.
- Retain existing history and use a guarded fast-forward update when publishing.
