COS - Colonial Observation Systems

See which Steam players are in your hex and track whether they are Colonial or Warden.

## What this is

COS watches Steam IPC log activity, resolves Steam profile names, and lets you tag players with:

- Faction (`Colonial` / `Warden`)
- Nicknames
- Custom colors
- Categories
- Notes

It also supports quick `/p` copying, bulk edits, import/export of aliases, and filtering inside the UI.

## Requirements

- Python 3.10+
- Steam
- A Steam Web API key

## Setup

1. Install dependencies:

```text
pip install -r requirements.txt
```

2. Copy `config.example.json` to `config.json`.

3. Edit `config.json` and fill in:

- `api_key`
- `log_path`

Example:

```json
{
  "api_key": "YOUR_STEAM_WEB_API_KEY",
  "log_path": "C:\\Program Files (x86)\\Steam\\logs\\ipc_SteamClient.log"
}
```

4. Open the Steam console:

- Press `Windows + R`
- Run `steam://open/console`
- Enter `log_ipc 1`

5. Start COS:

```text
python cos.py
```

Or run `start.bat`.

## GitHub notes

- `config.json` is intentionally gitignored because it contains your private API key and local log path.
- `aliases.json` is intentionally gitignored because it contains your local player data.
- Backup files and old OOBS-era files are also gitignored so the repo stays clean.

## Workflow

- When a player appears, COS lists them automatically.
- Press `/p` to copy a PM command for that player.
- Test the PM in Foxhole.
- Mark them as `Colonial` or `Warden` in COS.
- Use tags, notes, filters, bulk edit, and export/import to maintain your roster.
