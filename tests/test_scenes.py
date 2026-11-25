"""Scene records, built-in colors, and panel normalization."""

from __future__ import annotations

from custom_components.color_kinetics.effects import EFFECT_NAMES
from custom_components.color_kinetics.scenes import (
    ALL_BLUE,
    ALL_ORANGE,
    CAMP_WHITE,
    CHRISTMAS_GREEN,
    CHRISTMAS_RED,
    EFFECT_NONE,
    HALLOWEEN_PURPLE,
    MODE_EFFECT,
    SCENE_BLUE,
    SCENE_CAMP,
    SCENE_CHRISTMAS,
    SCENE_HALLOWEEN,
    SCENE_ORANGE,
    clamp_percent,
    default_panel,
    default_scenes,
    effect_options,
    find_scene_name,
    normalize_effect_option,
    normalize_panel,
    normalize_scene,
    normalize_scene_map,
    parse_color_text,
    parse_fixture_lines,
    percent_to_brightness,
    resolve_scene,
    scene_fixture_text,
)


def test_builtin_scenes_match_the_saved_colors():
    scenes = default_scenes()
    keys = ["one", "two", "three"]
    purple = resolve_scene(scenes[SCENE_HALLOWEEN], keys)
    assert [item[1] for item in purple] == [HALLOWEEN_PURPLE, HALLOWEEN_PURPLE, HALLOWEEN_PURPLE]
    assert purple[0][2] == 100

    holiday = resolve_scene(scenes[SCENE_CHRISTMAS], keys)
    assert [item[1] for item in holiday] == [CHRISTMAS_RED, CHRISTMAS_GREEN, CHRISTMAS_RED]
    continued = resolve_scene(scenes[SCENE_CHRISTMAS], ["four"], offset=3)
    assert continued[0][1] == CHRISTMAS_GREEN

    assert resolve_scene(scenes[SCENE_BLUE], ["one"])[0][1] == ALL_BLUE
    assert resolve_scene(scenes[SCENE_CAMP], ["one"])[0][1] == CAMP_WHITE
    assert resolve_scene(scenes[SCENE_ORANGE], ["one"])[0][1] == ALL_ORANGE
    assert set(scenes) == {
        SCENE_HALLOWEEN,
        SCENE_CHRISTMAS,
        SCENE_BLUE,
        SCENE_CAMP,
        SCENE_ORANGE,
    }


def test_fixture_lines_override_the_pattern_and_bad_lines_are_rejected():
    scene = {
        "pattern": "alternate",
        "colors": [[255, 0, 0], [0, 255, 0]],
        "brightness": 80,
        "fixtures": {"880055BE": {"rgb": [0, 0, 255], "brightness": 40}},
    }
    assigned = resolve_scene(normalize_scene(scene), ["880055BE", "88002CD9"])
    assert assigned[0] == ("880055BE", (0, 0, 255), 40)
    assert assigned[1] == ("88002CD9", (0, 255, 0), 80)
    assert "880055BE, 0, 0, 255, 40" in scene_fixture_text(scene, ["88002CD9", "880055BE"])

    parsed, error = parse_fixture_lines("880055BE, 1, 2, 3, 50\n\n# comment\n")
    assert error is None
    assert parsed["880055BE"]["rgb"] == [1, 2, 3]
    assert parsed["880055BE"]["brightness"] == 50
    assert parse_fixture_lines("nope")[1]
    assert parse_fixture_lines("880055BE, 300, 0, 0")[1]
    assert parse_fixture_lines("880055BE, 1, 2, 3, 150")[1]
    assert parse_fixture_lines("880055BE, 1, 2, 3, no")[1]
    assert parse_color_text("238, 0, 255") == HALLOWEEN_PURPLE
    assert parse_color_text("") is None
    assert parse_color_text("1, 2") is None
    assert parse_color_text("1, 2, no") is None


def test_panel_and_effect_names_reject_unknown_values():
    assert effect_options()[0] == EFFECT_NONE
    assert effect_options()[1:] == list(EFFECT_NAMES)
    assert normalize_effect_option(" none ") == EFFECT_NONE
    assert normalize_effect_option("Rainbow chase") == "Rainbow cycle"
    assert normalize_effect_option("nope") == EFFECT_NONE
    assert clamp_percent("nope") == 100
    assert clamp_percent(-4) == 0
    assert percent_to_brightness(100) == 255
    assert percent_to_brightness(0) == 0
    assert percent_to_brightness(50) == 128

    panel = normalize_panel(
        {"effect": "Rainbow", "mode": "effect", "brightness": 40, "show_origin": "12.5", "show_speed": 5}
    )
    assert panel["mode"] == MODE_EFFECT
    assert panel["effect"] == "Rainbow"
    assert panel["brightness"] == 40
    assert panel["show_origin"] == 12.5
    assert normalize_panel({"mode": "effect"})["mode"] == "idle"
    assert normalize_panel({"mode": "scene"})["mode"] == "idle"
    assert normalize_panel({"mode": "nope"})["mode"] == "idle"
    assert normalize_panel({"show_origin": "nope", "show_speed": "nope"})["show_origin"] is None
    assert normalize_panel(None) == default_panel()
    assert normalize_scene(None) is None
    assert normalize_scene({"pattern": "solid"}) is None
    assert normalize_scene({"pattern": "alternate", "colors": [[1, 2, 3]]}) is None
    assert normalize_scene({"pattern": "nope"}) is None
    assert normalize_scene_map({"  Camp  ": default_scenes()[SCENE_CAMP], "": {}, "bad": "nope"})
    assert find_scene_name(default_scenes(), " camp ") == SCENE_CAMP
    assert find_scene_name(default_scenes(), "   ") is None
    assert resolve_scene({"pattern": "fixtures", "fixtures": {}}, ["a"]) == []
