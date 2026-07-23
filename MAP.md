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
- bridge_state_store.py — atomic JSON persist for BridgeState.
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
- Unsolicited delivery target = last real chat id (`_pending_chat_id`); if None, WARNING + drain + drop.
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
