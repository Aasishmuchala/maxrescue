"""Settings.

Load is deliberately unbreakable: a tool that refuses to run because of its own
config file is worse than one that runs with sensible numbers.
"""

from __future__ import annotations

import json

from maxrescue.app.settings import Settings


def _write(tmp_path, payload) -> str:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload)
    return str(path)


def test_defaults_load_when_there_is_no_file(tmp_path):
    settings = Settings.load(str(tmp_path / "absent.json"))
    assert settings.ram_budget_gb == 70.0
    assert settings.convert_bitmaps is False


def test_valid_values_are_taken(tmp_path):
    path = _write(tmp_path, {"ram_budget_gb": 48, "per_node_face_threshold": 50_000})
    settings = Settings.load(path)
    assert settings.ram_budget_gb == 48.0
    assert settings.per_node_face_threshold == 50_000


def test_a_json_file_that_is_not_an_object_falls_back_to_defaults(tmp_path):
    for payload in (["a", "list"], "just a string", 42):
        settings = Settings.load(_write(tmp_path, payload))
        assert settings.ram_budget_gb == 70.0


def test_unparseable_json_falls_back_to_defaults(tmp_path):
    assert Settings.load(_write(tmp_path, "{not json at all")).ram_budget_gb == 70.0


def test_a_bad_field_is_ignored_without_losing_the_good_ones(tmp_path):
    path = _write(
        tmp_path,
        {"ram_budget_gb": "seventy", "per_node_face_threshold": 44_000},
    )
    settings = Settings.load(path)
    assert settings.ram_budget_gb == 70.0
    assert settings.per_node_face_threshold == 44_000


def test_negative_and_zero_numbers_are_refused(tmp_path):
    settings = Settings.load(
        _write(tmp_path, {"ram_budget_gb": -5, "per_node_face_threshold": 0})
    )
    assert settings.ram_budget_gb == 70.0
    assert settings.per_node_face_threshold == 30_000


def test_a_bool_field_will_not_accept_a_number(tmp_path):
    """`convert_bitmaps: 1` must not silently enable a stage that changes the
    render."""
    settings = Settings.load(_write(tmp_path, {"convert_bitmaps": 1}))
    assert settings.convert_bitmaps is False


def test_a_blank_string_does_not_replace_a_path(tmp_path):
    settings = Settings.load(_write(tmp_path, {"proxy_out_dir": "   "}))
    assert settings.proxy_out_dir == "proxies"


def test_unknown_keys_are_ignored(tmp_path):
    settings = Settings.load(_write(tmp_path, {"from_a_future_version": True}))
    assert settings.ram_budget_gb == 70.0


def test_save_never_raises_even_on_an_unwritable_path():
    Settings().save("/definitely/not/a/writable/path/settings.json")


def test_the_plan_config_projection_carries_the_settings_through():
    settings = Settings(per_node_face_threshold=12_345, convert_bitmaps=True)
    config = settings.plan_config()
    assert config.per_node_face_threshold == 12_345
    assert config.convert_bitmaps is True


def test_the_ram_budget_is_exposed_in_bytes():
    assert Settings(ram_budget_gb=2).ram_budget_bytes == 2 * (1 << 30)
