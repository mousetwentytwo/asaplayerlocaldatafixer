# asaplayerlocaldatafixer
Tool to help clear stuck Items and Dinos from your local transfer save file of ARK: Survival Ascended.


Fix corrupt **ARK: Survival Ascended** `PlayerLocalData.arkprofile` save files — specifically invisible/phantom items in the ARK inventory and broken or uninteractable tamed dinos.

The tool parses the UE5 binary property format into Python dicts/JSON so you can inspect, selectively clear, or repair the problematic entries, then serialises everything back to a byte-perfect `.arkprofile` binary.

### Common problems this aims to solve

- **Invisible / phantom ARK items** — items that occupy slots but can't be seen, moved, or used.
- **Broken tamed dinos** — dinos that appear in the upload list but can't be downloaded or interacted with.
- **Corrupted upload data** — stale or malformed entries left behind after transfers, server crashes, or rollbacks.
- **Inspect upload data** — check what is actually in the file without loading it into the game.

## Usage

Single script, subcommands (defaults to `gui` when none given):

```bash
python asa_tool_localprofile.py                                      # opens GUI
python asa_tool_localprofile.py extract PlayerLocalData.arkprofile   # → JSON
python asa_tool_localprofile.py build   PlayerLocalData.arkprofile.json  # → .arkprofile
python asa_tool_localprofile.py verify  PlayerLocalData.arkprofile   # validate sizes
```

| Subcommand | Args | Notes |
|---|---|---|
| *(none)* / `gui` | — | Tk-based profile editor (default) |
| `extract` | `<input> [-o out.json] [--indent N]` | Defaults output to `<input>.json` |
| `build` | `<input.json> [-o out.arkprofile]` | Strips `.json` for default output |
| `verify` | `<file> ... [-v]` | `-v` for verbose per-property output |

## [Library](asaplayerlocaldatafixer/README.md)

## Building a standalone exe

Requires [PyInstaller](https://pyinstaller.org/):

```bash
pip install pyinstaller
pyinstaller asa_tool_localprofile.spec
```

Output: `dist/asa_tool_localprofile.exe` — runs without Python installed. Double-click to open the GUI. 

## Credits and License

- **mousetwentytwo** — ark player facing a bug and creating a solution for it
- **Claude Opus 4.6** (Anthropic) — AI pair-programming assistant co-author

Unlicense (public domain) — see [LICENSE](LICENSE)
