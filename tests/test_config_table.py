"""Guards that keep synapse_core/config.default.toml the only defaults table."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from synapse_core import config

REPO = Path(__file__).resolve().parents[1]
PACKAGES = ("synapse_core", "synapse_tg", "synapse_wx")

# A literal fallback on a dict read is how a second copy of a default sneaks
# back in. Checked in the modules that parse config TOML; a non-config dict in
# one of them opts out with a trailing `# config-fallback-ok`.
FALLBACK_RE = re.compile(
    r"""\.get\(\s*["'][a-z_]+["']\s*,\s*(?:-?\d|["']|True|False|\{\}|\[\])"""
)
OPT_OUT = "# config-fallback-ok"

# The only modules allowed to parse a TOML config. Everything else takes its
# values from a resolved view.
CONFIG_MODULES = (
    "synapse_core/config.py",
    "synapse_core/upstream.py",
    "synapse_core/breaker.py",
    "synapse_tg/config.py",
    "synapse_wx/config.py",
)


def _sources() -> list[Path]:
    return sorted(p for pkg in PACKAGES for p in (REPO / pkg).rglob("*.py"))


def test_config_reads_have_no_literal_fallbacks() -> None:
    offenders = []
    for rel in CONFIG_MODULES:
        for n, line in enumerate((REPO / rel).read_text().splitlines(), 1):
            if FALLBACK_RE.search(line) and OPT_OUT not in line:
                offenders.append(f"{rel}:{n}: {line.strip()}")
    assert not offenders, (
        "literal default outside config.default.toml:\n" + "\n".join(offenders))


def test_only_config_modules_parse_toml() -> None:
    offenders = [
        str(path.relative_to(REPO))
        for path in _sources()
        if "tomllib" in path.read_text()
        and str(path.relative_to(REPO)) not in CONFIG_MODULES
    ]
    assert not offenders, f"parses TOML outside the config layer: {offenders}"


def test_every_table_key_is_read_by_the_packages() -> None:
    blob = "\n".join(p.read_text() for p in _sources())
    unused = [attr for attr in config.FIELDS
              if attr not in ("cwd_presets", "ack_overrides")
              and not re.search(rf"\b{re.escape(attr)}\b", blob)]
    assert not unused, f"defaults table keys nothing reads: {unused}"


def test_every_table_entry_has_a_field_and_vice_versa() -> None:
    table = config.defaults()
    declared = {(s, k) for s, k, _ in config.FIELDS.values()}
    present = {(section, key)
               for section, block in table.items()
               for key in (block or {"": None})}
    present = {(s, k if k else "") for s, k in present}
    assert declared == present, (
        f"only in FIELDS: {sorted(declared - present)}; "
        f"only in the table: {sorted(present - declared)}")


@pytest.mark.parametrize("channel", config.CHANNELS)
def test_empty_user_file_resolves_every_key(channel: str, tmp_path: Path) -> None:
    empty = tmp_path / "config.toml"
    empty.write_text("")
    values, explicit = config.resolve(channel, path=empty)
    expected = {a for a, (_, _, chans) in config.FIELDS.items() if channel in chans}
    assert set(values) == expected
    assert explicit == set()
    assert all(v is not None or a in ("cwd", "chat_id")
               for a, v in values.items())


@pytest.mark.parametrize("channel", config.CHANNELS)
def test_missing_user_file_matches_empty_user_file(channel: str, tmp_path: Path) -> None:
    absent, _ = config.resolve(channel, path=tmp_path / "nope.toml")
    empty_file = tmp_path / "config.toml"
    empty_file.write_text("")
    empty, _ = config.resolve(channel, path=empty_file)
    assert absent == empty


def test_default_table_is_valid_toml_and_has_no_duplicate_keys() -> None:
    tomllib.loads(config.DEFAULTS_PATH.read_bytes().decode("utf-8"))


def test_legacy_session_section_is_migrated(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(
        "[session]\n"
        'marrow_db_path = "/tmp/x.db"\n'
        'cc_projects_dir = "/tmp/projects"\n'
        'clear_default_model = "opus"\n'
        'session_record_command = "custom"\n'
    )
    values, explicit = config.resolve("wx", path=p)
    assert values["marrow_db"] == "/tmp/x.db"
    assert values["cc_projects_dir"] == "/tmp/projects"
    assert values["clear_default_model"] == "opus"
    assert values["session_record_command"] == "custom"
    assert {"marrow_db", "cc_projects_dir", "clear_default_model"} <= explicit


def test_new_spelling_wins_over_the_legacy_one(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('[marrow]\ndb = "/tmp/new.db"\n\n[session]\nmarrow_db_path = "/tmp/old.db"\n')
    values, _ = config.resolve("wx", path=p)
    assert values["marrow_db"] == "/tmp/new.db"


def test_bad_type_and_out_of_range_values_keep_the_default(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('[loop]\nbubble_cap = 0\npoll_interval_sec = "fast"\n')
    values, _ = config.resolve("wx", path=p)
    assert values["bubble_cap"] == config.default_value("bubble_cap")
    assert values["poll_interval_sec"] == config.default_value("poll_interval_sec")


def test_unknown_override_is_rejected() -> None:
    with pytest.raises(TypeError):
        config.resolve("tg", overrides={"not_a_key": 1})


def test_view_rejects_unknown_attribute() -> None:
    from synapse_tg.config import TgConfig

    with pytest.raises(AttributeError):
        TgConfig().nope
