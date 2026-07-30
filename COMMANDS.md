# Synapse — Commands

> Shared registry from synapse_core. Channel-unique marked [tg]/[wx].

## Slash

- /clear (alias /new) — fresh session, keeps current model
- /stop — interrupt, keep sid
- /model <id|alias> — swap model, keep sid; becomes the new default (survives restart)
- /info (alias /status, /usage) — model | effort | health / sid | uptime | ctx
- /help — render this file
- /thinking on|off — emit thinking block per turn
- /quote on|off — prepend quote block for cc quote tags
- /effort [low|medium|high|xhigh|max|auto] — set thinking-budget on next swap
- /voice cn|en — swap ack-string style. Persisted
- /cwd [N|<path>] — show/switch cwd + presets. Switch implies /clear. Persisted
- /resume [N|<sid>] — session picker, replay, cwd resolve, model restore
- /rewind <N> — truncate last N turns, respawn with --resume
- /regen — truncate + replay last user prompt
- /compact — [wx] cc protocol pipe. [tg] pending
- /diary [date] — fetch diary by date, inject as context
- /switch — [cc cli only] cross-channel session picker
- /tts off|on|auto — [tg] voice reply toggle
- /tl [hint] — record a timeline line now (marrow `tl` action=add)
- /tl- — silence this session: mute tl (action=add) nudge + stop self writes (dies with session)

## Bare commands (no /)

- mm- / mm+ — block/unblock session in marrow audit_log

## Aliases (no /)

- 5 / fable → Fable 5
- 4.6 / 4.7 / 4.8 / opus → Opus [1m]
- sonnet → Sonnet 4.6
- haiku → Haiku 4.5

## Cortex circuit breaker (not a bridge command)

- Stops cortex autonomous activity (fed rounds / auto wake). Bridge and normal chat unaffected.
- Persistent across restarts. State: `~/.config/marrow/breaker.json` (MAP.md §9.1).
- On: `/ct-pause` in cc cli (`cortex.ctl pause [--shell cli|tg]`, scopes merge).
- Off: `/ct-wake [tg|cli|all]` (clear + kick that shell now) or `cortex.ctl resume [--shell]` (clear only). Show: `cortex.ctl status`.
- Auto-trips after `[cortex.breaker].fuse_threshold` fuses within `window_hours` (marrow config.toml).

## Hold words

- 等 / 稍等 / 等等 / 先 — hold 10s instead of 5s before flush
- trailing ... / …… / 。。。/ ～ — same 10s hold
