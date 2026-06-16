# Synapse

Channel-agnostic LLM bridge for Claude Code — Telegram + WeChat, with optional marrow memory.

## Quick start — Telegram

1. Fork + clone
2. `pip install -e ".[tg]"` (or `uv sync --extra tg`)
3. `cp config.toml.example ~/.config/synapse-tg/config.toml`
4. Fill in `[bot] token` (from @BotFather)
5. `python -m synapse_tg`

## Quick start — WeChat

1. Fork + clone
2. `pip install -e ".[wx]"`
3. `python -c "from synapse_wx.ilink import ILinkClient; ILinkClient().login()"` — scan QR, note your wxid
4. `cp config.toml.example ~/.config/synapse-wx/config.toml`
5. Fill in `[user] target_wxid`
6. `python -m synapse_wx`

## Marrow integration (optional)

Set `marrow_bridge = true` in `[provider]` and fill in `[marrow]` section.
Requires [marrow](https://github.com/Jaynechu/marrow) installed separately.

## Configuration

`config.toml.example` — all sections annotated. Key sections:

- `[provider]` — cc path, cwd, marrow toggle
- `[persona]` — user/assistant display names
- `[cwd_presets]` — numbered shortcuts for `/cwd N`
- `[marrow]` — db path + sessionend command template

## Commands

See `COMMANDS.md` for the full list. Core commands: `/clear`, `/resume`, `/model`, `/info`,
`/thinking`, `/effort`, `/cwd`, `/rewind`, `/regen`, `/help`.

## Channels

- `synapse_core/` — shared library: command registry, providers, session lifecycle
- `synapse_tg/` — Telegram bridge (python-telegram-bot)
- `synapse_wx/` — WeChat bridge (iLink HTTP)

## Running as a service (macOS)

Templates in `deploy/`. Copy, fill in your paths, then:

```sh
cp deploy/com.synapse-wx.bridge.plist.template ~/Library/LaunchAgents/com.synapse-wx.bridge.plist
# edit WorkingDirectory + ProgramArguments
launchctl load ~/Library/LaunchAgents/com.synapse-wx.bridge.plist
```

Telegram equivalent: create a plist following the same pattern with `python -m synapse_tg`.

## Requirements

- macOS
- Python 3.12+
- Claude Code CLI (`claude` on PATH, authenticated)
- Channel token: Telegram bot token **or** WeChat iLink QR login

## License

MIT
