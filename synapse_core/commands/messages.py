"""User-facing ack strings — bilingual lookup table.

All bridge -> channel short replies (command acks, fallback bubbles, restart
self-announce) go through ``t(key, style, **vars)``. Inline f-string acks are
forbidden — when adding a new ack, register the key here in BOTH ``cn`` and
``en`` first.

Style:
  - ``cn`` — 中文搞笑：撒娇/吐槽/黑色幽默, emoji ok, ≤ 25 chars.
  - ``en`` — English short：success ``Verb + STATE``; failure
    ``Command: /xxx <...> (now: {x})``; ≤ 6 words.

Persisted on BridgeState.voice_style; default ``cn``.

Full contract lives in MAP.md §3 Outbound — read it before changing ack
behaviour here or in registry.py. PRs with inline f-string acks get rejected.
"""

from __future__ import annotations

from typing import Final

STYLES: Final[tuple[str, ...]] = ("cn", "en")
DEFAULT_STYLE: Final[str] = "cn"

# key -> {style -> template}.  Templates use str.format placeholders.
MESSAGES: Final[dict[str, dict[str, str]]] = {
    # ── /model ──────────────────────────────────────────────────
    "model.ok": {
        "cn": "🤖({name})上线中...",
        "en": "Model: {name}",
    },
    "model.same": {
        "cn": "🤖是我是我还是我！",
        "en": "Already {name}",
    },
    "model.usage": {
        "cn": "查无此机，请重新输入：\n/model <5|4.6|4.7|4.8|fable|sonnet|haiku|opus|claude-...>",
        "en": "Command: /model <5|4.6|4.7|4.8|fable|sonnet|haiku|opus|claude-...>",
    },

    # ── /clear ──────────────────────────────────────────────────
    "clear.ok": {
        "cn": "新鸭上桌🦆 ({name})",
        "en": "New session ({name})",
    },

    # ── /stop ───────────────────────────────────────────────────
    "stop.ok": {
        "cn": "🛑施法已打断",
        "en": "Stopped, session kept",
    },

    # ── /help ───────────────────────────────────────────────────
    "help.missing": {
        "cn": "😭小抄找不到了！！",
        "en": "COMMANDS.md not found",
    },

    # ── /resume ─────────────────────────────────────────────────
    "resume.ok": {
        "cn": "🧚‍♀️本机已复活: {sid} | {name}",
        "en": "Resumed: {sid} | {name}",
    },
    "resume.cwd_switched": {
        "cn": "(搬好家啦，这次在 {dir} 聊🏠)",
        "en": "Resumed in {dir}",
    },
    "resume.empty": {
        "cn": "😤最近没找我吧？",
        "en": "No recent sessions",
    },
    "resume.no_n": {
        "cn": "🙂‍↔️你要的太多了",
        "en": "No session {n}",
    },

    # ── /rewind ─────────────────────────────────────────────────
    "rewind.ok": {
        "cn": "🧠失忆中，请稍候...({n})",
        "en": "Rewinding {n}…",
    },
    "rewind.usage": {
        "cn": "😤刷几条？ /rewind <N>",
        "en": "Command: /rewind <N>",
    },
    "rewind.bad_n": {
        "cn": "N 得是正整数",
        "en": "N must be positive int",
    },
    "rewind.no_sess": {
        "cn": "🤷‍♀️无事可忘",
        "en": "No session yet",
    },

    # ── /regen ──────────────────────────────────────────────────
    "regen.ok": {
        "cn": "🧠失忆中，请稍候...",
        "en": "Regenerating…",
    },
    "regen.no_sess": {
        "cn": "🤷‍♀️无事可忘",
        "en": "No session yet",
    },

    # ── /thinking ───────────────────────────────────────────────
    "thinking.on": {
        "cn": "又来偷窥思考链？好吧好吧给你看😌",
        "en": "Thinking ON",
    },
    "thinking.off": {
        "cn": "不看就不看😤",
        "en": "Thinking OFF",
    },
    "thinking.usage": {
        "cn": "到底看不看？/thinking <on|off> (现在:{x})",
        "en": "Command: /thinking <on|off> (now: {x})",
    },

    # ── /quote ──────────────────────────────────────────────────
    "quote.on": {
        "cn": "引用已打开",
        "en": "Quote ON",
    },
    "quote.off": {
        "cn": "引用已关闭",
        "en": "Quote OFF",
    },
    "quote.usage": {
        "cn": "开还是关？ /quote <on|off> (现在:{x})",
        "en": "Command: /quote <on|off> (now: {x})",
    },

    # ── /effort ─────────────────────────────────────────────────
    "effort.ok": {
        "cn": "🐮🐴 {level} 档",
        "en": "Effort: {level} (next swap)",
    },
    "effort.usage": {
        "cn": "请输入🐮🐴等级？ /effort <low|medium|high|xhigh|max|ultracode|auto> (现在:{x})",
        "en": "Command: /effort <low|medium|high|xhigh|max|ultracode|auto> (now: {x})",
    },

    # ── /compact ────────────────────────────────────────────────
    "compact.ok": {
        "cn": "在压了...",
        "en": "Compacting…",
    },
    "compact.no_cc": {
        "cn": "cc没跑呢",
        "en": "[compact] cc not running",
    },
    "compact.no_pipe": {
        "cn": "这个provider不支持pipe",
        "en": "[compact] provider does not support pipe",
    },
    "compact.piped": {
        "cn": "已丢给cc压缩",
        "en": "[compact] /compact piped to cc",
    },
    "compact.no_sess": {
        "cn": "没东西压！",
        "en": "No session yet",
    },
    "compact.fail": {
        "cn": "压缩管道炸了💥：{error}",
        "en": "Compact failed: {error}; try /clear",
    },

    # ── mm- / mm+ ───────────────────────────────────────────────
    "mm.block": {
        "cn": "本窗口跳过DB",
        "en": "Session skipped",
    },
    "mm.block_no_sess": {
        "cn": "无会话",
        "en": "No session yet",
    },
    "mm.clear": {
        "cn": "本窗口加入DB",
        "en": "Session added",
    },
    "mm.clear_no_sess": {
        "cn": "无会话",
        "en": "No session yet",
    },

    # ── /diary ──────────────────────────────────────────────────
    "diary.noparam": {
        "cn": "(看哪天的日记呀？e.g. /diary 前天)",
        "en": "Which day? e.g. /diary yesterday",
    },
    "diary.ok": {
        "cn": "📖 {date}",
        "en": "📖 {date}",
    },
    "diary.empty": {
        "cn": "({date} 没有日记)",
        "en": "No diary for {date}",
    },
    "diary.unavail": {
        "cn": "(diary 未接入)",
        "en": "Diary not available",
    },

    # ── unknown ─────────────────────────────────────────────────
    "unknown.cmd": {
        "cn": "啥玩意？没见过啊。看看小抄 /help",
        "en": "Unknown command /help",
    },

    # ── /voice (the meta-command) ──────────────────────────────
    "voice.set": {
        # The ack for /voice <x> always renders in the NEW style (post-swap),
        # so each entry below is the "welcome to this voice" tagline.
        "cn": "🌪️一大波搞笑提示即将来袭",
        "en": "English notifications activated.",
    },
    "voice.same": {
        "cn": "🙄已经是 {x} 啦",
        "en": "Already {x}",
    },
    "voice.usage": {
        # Meta-command — a user trying /voice for the first time may not read
        # CN, so this hint stays English in BOTH styles with a Chinese label
        # so the choice is self-explanatory either way.
        "cn": "Set reply voice. /voice <cn|en>  (now:{x})  cn=funny 中文 · en=plain English",
        "en": "Set reply voice. /voice <cn|en>  (now:{x})  cn=funny 中文 · en=plain English",
    },

    # ── /cwd ────────────────────────────────────────────────────
    "cwd.ok": {
        "cn": "🚪任意门传送中: {name}",
        "en": "Cwd: {name}",
    },
    "cwd.show": {
        "cn": (
            "当前位置 {cur}\n请选择目的地:\n  1 → NY\n  2 → Study\n"
            "  3 → marrow\n（增减预设见 /help 或咨询家机）"
        ),
        "en": (
            "current: {cur}\npresets:\n  1 → NY\n  2 → Study\n"
            "  3 → marrow\n(see /help, or ask me to add)"
        ),
    },
    "cwd.not_found": {
        "cn": "🙅‍♀️此路不通",
        "en": "Path not found",
    },
    "cwd.not_dir": {
        "cn": "🙅‍♀️世上本没有路，你再怎么走，这也不是路",
        "en": "Not a directory",
    },
    "cwd.no_n": {
        "cn": "😅查无此号",
        "en": "No preset {n}",
    },

    # ── non-command bubbles ────────────────────────────────────
    "provider.dead": {
        "cn": "老公已死，有事烧token🪦",
        "en": "Provider dead.",
    },
    "provider.restarting": {
        "cn": "重启中，再说一次",
        "en": "[bridge: provider restarting, try again]",
    },
    "bridge.error": {
        "cn": "桥炸了，再试一次",
        "en": "[bridge: error, try again]",
    },
    "restart.bubble": {
        "cn": "我重启了",
        "en": "Restarted.",
    },
    "media.icloud_outbox": {
        "cn": "📦 已发货，请在{channel_label}签收 {name}",
        "en": "📦 Shipped to {channel_label}: {name}",
    },
}


def normalize_style(style: str | None) -> str:
    """Return a known style or fall back to DEFAULT_STYLE."""
    if style in STYLES:
        return style  # type: ignore[return-value]
    return DEFAULT_STYLE


def t(
    key: str,
    style: str | None = None,
    *,
    channel_label: str | None = None,
    **vars: object,
) -> str:
    """Render a user-facing ack string by key + style.

    Unknown key → KeyError (loud on purpose; new acks must register a key).
    Unknown style → falls back to DEFAULT_STYLE.
    Missing template var → KeyError from str.format (also loud).
    """
    entry = MESSAGES[key]
    s = normalize_style(style)
    template = entry.get(s) or entry.get(DEFAULT_STYLE) or next(iter(entry.values()))
    if "{channel_label}" in template:
        if channel_label is None:
            raise KeyError("channel_label")
        vars["channel_label"] = channel_label
    if not vars:
        return template
    return template.format(**vars)
