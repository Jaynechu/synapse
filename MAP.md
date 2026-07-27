# Synapse — MAP

> What's built + where. Code is SoT; this doc locates features without grepping.
> Goals → DESIGN. Commands → COMMANDS.

## 1. System map

```
[tg] TG ──▶ python-telegram-bot (long-poll)  ─┐
[wx] WX ──▶ ILink.poll_messages (HTTP poll)   ─┤
                                                ▼
         InboundBuffer (5s quiet / 10s hold) ──▶ time_anchor ──▶ MainLoop.tick
                                                  Registry (slash/alias/mm±) ─┬─ handler
                                                  forward ──▶ Provider.send ──┘
                                                  cc subprocess (stream-json, MARROW_BRIDGE=1)
[tg] bot.send_* ◀── split_for_tg ◀───────┐
[wx] ILink.send_* ◀── split_for_wechat ◀──┤◀── Provider.recv
                                           └◀── optional TTS pipeline [tg]

Side: SessionTracker ──▶ IdleFireLoop (mid_scan 30min) ──▶ popen mw mid_scan / sessionend_async
[wx]  SleepWake ──▶ pause/resume · HealthGate ──▶ AlertSink
```

Runtimes: bridge (launchd, single process) · cc subprocess (persistent, swap = close+respawn) · marrow mid_scan (30min tick, pre-archive + three-way trigger) · sessionend_async (detached one-shot).

## 2. Core modules (synapse_core/)

- state.py — BridgeState frozen dataclass: sid, model, effort, voice_style, thinking, cwd.
- debounce.py — InboundBuffer: 5s quiet window, hold-word (等/稍等) → 10s sticky.
- anchor.py — time_anchor (gap + timestamp prefix), quote_prefix (80-char cap).
- replay.py — replay last N turns from jsonl for /resume.
- jsonl_edit.py — atomic truncate for /rewind, /regen.
- marrow_session.py — record_session upsert, session_cwd resolve.
- bridge_state_store.py — atomic JSON persist for BridgeState. Includes state.model_resolved: `--model` token → id cc reported in system/init, written by both loops' init handler, read by /clear + /model acks (display only).
- last_active.py — {sid, channel, ts} stamp per prompt.
- health.py — HealthGate: dirty boot detection via boot_ts vs last clean shutdown.
- alerts.py — AlertSink: file-per-alert + optional mw add-alert.
- usage.py — cc /usage scrape for /info display.
- providers/base.py — Provider interface. providers/cc.py — stream-json subprocess. providers/mock.py — test double.
- commands/registry.py — dispatch hub: slash → digit → mm± → alias → forward.
- commands/handlers.py — CommandContext closures (swap/close/forget/respawn/replay/audit).
- commands/messages.py — t(key, style) cn/en ack pairs. Only path for user-facing acks.
- commands/aliases.py — MODEL_ALIASES (5/fable/opus/sonnet/haiku).
- commands/marrow_audit.py — mm-/mm+ direct sqlite to marrow.db.
- shell_state.py — per-shell cortex ledger `<state_dir>/<shell>.json` (flock + atomic replace). Protocol shared with marrow/cortex, code never imported across repos.
- breaker.py — circuit breaker, bridge side (§9.1).
- sessionend/tracker.py — SessionTracker: sessions.json, RLock + atomic write.
- sessionend/idle.py — IdleFireLoop: 30min scan, cross-channel cleanup, mid_scan subprocess spawn, .mid_fired markers.

## 3. Inbound

- Shared: InboundBuffer → time_anchor → channel_marker [channel: xx] per prompt.
- [tg] python-telegram-bot Update handler — text, voice, photo, document, video, sticker. File API download to tmp. Voice OGG → cc transcribe. Sticker webp materialize. Quote: native reply_to_message → [quoting: "..."].
- [wx] ILink.poll_messages (1s, retry-wrapped). Cursor atomic tmp+rename. Media: AES-128-ECB decrypt (3 key-shape fallbacks). PDF >20p: pdftotext → markitdown. Sticker caption routing: 0=suppress, 1=sticker-save, 1+text=ingest. Quote: iLink reference → [quoting: "..."].

## 4. Outbound

- [tg] split_for_tg — 4096-char, paragraph split. Streaming: edit_message_text ~1s/200ch throttle → final gfm_to_tg_html. Thinking: full text. Media: bot.send_photo/document/animation/video (TG CDN, no AES). Sticker: bot.send_sticker (webp).
- [wx] split_for_wechat — 200-char paragraph/sentence split. Media upload two-step CDN (getuploadurl → AES-128-ECB POST). CDN quirks: MicroMessenger UA required, ~1/3 flaky → 3 retries. Image downscale ≤250KB via sips. 550KB ceiling (chunked = FUTURE). Thinking: one bubble, full text.
- Ack strings: messages.py t(key, style) — cn/en pairs mandatory. Style persisted in BridgeState.voice_style.

## 5. Resident listener (unsolicited turns)

- Turn classification: first event `system(task_notification)` = unsolicited turn (notification frame yields no text); consecutive unsolicited turns possible (multiple background agents).
- Provider (`synapse_core/providers/cc.py`): `poll_line(timeout)` — no liveness clock, `POLL_EOF` sentinel + `alive=False` on reader EOF; `recv(first_line=...)` processes a pre-read line before the queue.
- Shared `_deliver_reply` — turn-aware stream/drain delivers unsolicited turns inline; solicited turn returned to flush as normal.
- Resident idle listener: [tg] asyncio task under the flush `asyncio.Lock`, started post_init. [wx] daemon thread. Delivers background-task answers between turns; typing indicator runs during generation. Lazy respawn on EOF only — listener never respawns.
- Lock discipline: [tg] one asyncio.Lock serializes flush+listener. [wx] `_state_lock` is never held across recv; dedicated `_recv_lock` is the single-consumer guarantee on the provider stdout queue — flush holds it across send→drain→retry, listener across poll+drain. Strict ordering: `_recv_lock` outer, `_state_lock` inner. Any future stdout-queue consumer must take `_recv_lock`.
- Bridge-initiated delivery target (`_outbound_target`, used by shell `feed_turn` + idle listener) = live chat (`_pending_chat_id`), else config `[tg].chat_id`; bot seeded post_init via `attach_bot` so a restart delivers before any inbound message. Both None → WARNING + drain + drop.
- Storm alert `bridge_turn_storm` when >`unsolicited_storm_cap` (config, default 5) unsolicited turns land in one lock-hold.

## 6. Commands

- Dispatch: slash → picker digit → mm± bare → MODEL_ALIASES → forward.
- Key handlers: /clear (session close + sessionend fire), /resume (tri-mode: list/pick/direct + cross-project cwd resolve), /rewind + /regen (jsonl truncate + respawn), /cwd (preset switch, implicit /clear).
- [tg] unique: /tts off|on|auto (voice reply). Inline keyboard for /resume picker.
- [wx] unique: /switch (cross-channel session picker). /compact (cc protocol pipe).
- Full list → COMMANDS.md.

## 7. Session lifecycle

- SessionTracker (sessions.json, atomic write) — set on system{init}, forget on /clear.
- IdleFireLoop (30min scan) — cross-channel cleanup (claimed_away_hook + replay_bookmark); mid_scan three-way trigger (4h+10turns / 30turns+2h / 6h+4turns) via marrow.mid_scan subprocess.
- record_session → marrow.sessions upsert. Any bridge session visible to cli /switch.
- Boot resume: snapshot → state.session_id if jsonl exists.
- MARROW_BRIDGE=1 → marrow SessionEnd hook defers to bridge (bridge_owns marker, 12h TTL fallback).
- [wx] SleepWake: pyobjc will-sleep/did-wake → pause/reconnect/catchup.

## 8. TTS voice pipeline [tg]

- Pipeline: text → TTS provider → OGG Opus → bot.send_voice.
- Cascade: Qwen3 (free, best CN) → Volcengine (paid, ~300ms) → Edge-TTS (free, ~400ms).
- Toggle: /tts off (default) | on | auto (>N chars). Config-driven provider selection.

## 9. Safety nets

- AlertSink: file per alert + optional mw add-alert.
- HealthGate: dirty boot detection → alert.
- Provider death gate: session_id set = fake (swap killed), empty = real → critical.
- [tg] Retry: python-telegram-bot built-in + custom backoff.
- [wx] iLink retry: @with_retry exp backoff cap 5. SleepWakeObserver. cc stderr drain (deadlock prevention).
- Launchd KeepAlive + 30s throttle (both channels).

### 9.1 Circuit breaker (`synapse_core/breaker.py`) — the cortex main switch

- ONE persistent file stops the cortex shell's AUTONOMOUS activity (fed rounds / respawn-driven rounds) for the shells it covers. **The bridge itself keeps running and normal tg/wx chat is completely unaffected** — inbound messages flow, sessions spawn from user turns as usual. Survives every restart; only an explicit clear releases it.
- **State file** `<marrow config dir>/breaker.json` (= parent of `[marrow].db`, `TgConfig.marrow_config_dir()`):
  `{"scope": "all"|"cli"|"tg", "reason": "auto_fuse"|"manual", "ts": "<local iso>"}`.
  File ABSENT = clear. Corrupt / wrong-shape / empty scope = read as CLEAR + one warning (a broken breaker must never wedge the bridge). flock on a `.lock` sibling, tmp+`os.replace` write.
- **Fuse tally** `<marrow config dir>/fuse_events.json`: `{"events": [{"ts": "<iso>", "shell": "cli"|"tg"}, ...]}`. BOTH shells append here; entries older than `window_hours` are pruned on every write, so the post-write length IS the rolling cross-shell count.
- **The JSON file IS the cross-repo protocol** — cortex ships its own independent copy (`cortex/breaker.py`). Schema shared, code never imported (same rule as shell_state.py).
- **Config: marrow only.** `[cortex.breaker]` in `~/.config/marrow/config.toml` (`enabled` / `fuse_threshold` / `window_hours` / `trip_message` / `clear_message`), read directly by `breaker.settings()`. Deliberately NOT duplicated into the tg bridge config.
- **Choke point (tg)**: `ShellHost._fire` returns before feeding when `_breaker_holds()`. The ledger (`next_wake_at` / `pending_note` / `rotate_pending`) is left INTACT, so whatever was due delivers on the first round after a clear. It re-arms one idle window into the future so a past-due deadline cannot spin the scheduler.
- **Auto trip (tg)**: `ShellHost.after_turn`'s fuse branch calls `_record_fuse()` first → `breaker.record_fuse_and_maybe_trip(dir, "tg")`. Count >= `fuse_threshold` and `enabled` → scope="all", reason="auto_fuse"; `enabled = false` still tallies, never trips. On trip: a `critical` / `cortex_breaker_tripped` row via the loop's AlertSink plus a direct `bot.send_message` notice. The wrap-up FUSE prompt is then skipped (it is an autonomous feed), but `shell_respawn()` still runs — it only drops the oversized session; a fresh one is created by the next inbound user message.
- Clearing is cortex-side only (`ct-wake` / `cortex.ctl resume`) — the bridge never clears the breaker.
- Layering: the breaker is the OPERATIONAL switch. `[cortex].shells` in marrow config (single source, T7: `TgConfig.shell_active()` reads it directly, no local enable flag) is DEVELOPER-LAYER wiring ("is this shell installed at all") — not the way to pause or disable cortex.

## 10. Config and paths

- Data dir: ~/.config/synapse-{tg,wx}/ (alerts, health, sessions.json, bridge_state.json).
- Logs: ~/Library/Logs/synapse-{tg,wx}.{out,err}.log.
- Config: config.toml per channel (defaults in config.py). Template → config.toml.example.
- Auth: [tg] bot token in config. [wx] iLink QR → token.json.
- Plist: com.synapse-{tg,wx}.bridge. Template in deploy/.

## 11. Known gaps

- [wx] CDN media send failures are log-only, no AlertSink.
- [wx] 550KB upload ceiling; chunked upload = FUTURE.
- [tg] /compact not wired (cc protocol pipe pending).
- [tg] Quote outbound (reply_to_message_id) not tracked.
- [both] record_effort callable not wired for /effort → marrow sessions.
