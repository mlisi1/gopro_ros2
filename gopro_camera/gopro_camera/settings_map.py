"""ROS parameter names <-> Open GoPro setting IDs / options, per camera model.

All IDs, option values, labels and per-model support come from ``spec_tables.py``, which is generated
from the official Open GoPro OpenAPI spec (``scripts/generate_spec_tables.py``). Nothing here is
hardcoded from memory.

Parameter convention (``settings.<name>``, dynamically typed):

- integer value  -> the spec option ID (e.g. ``settings.video_resolution: 1``)
- string value   -> the spec option label, case/space-insensitive (e.g. ``"4K"``, ``"60"``, ``"Linear"``);
  a numeric string that matches no label is treated as an option ID.
- unset          -> the driver does not touch this setting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from gopro_camera.errors import InvalidSetting
from gopro_camera.spec_tables import CAMERAS, MIN_FIRMWARE, SETTINGS

PARAM_PREFIX = "settings."


@dataclass(frozen=True)
class SettingDef:
    id: int
    name: str
    title: str
    description: str
    options: dict[int, str]
    value_range: tuple[int, int] | None
    option_cameras: dict[int, list[str]] = field(default_factory=dict)
    cameras: list[str] = field(default_factory=list)

    @property
    def param_name(self) -> str:
        return PARAM_PREFIX + self.name

    @property
    def is_enumerated(self) -> bool:
        return bool(self.options)

    def label(self, option: int) -> str:
        return self.options.get(option, str(option))


def _build_defs() -> dict[int, SettingDef]:
    defs: dict[int, SettingDef] = {}
    for sid, d in SETTINGS.items():
        defs[sid] = SettingDef(
            id=sid,
            name=d["name"],
            title=d["title"],
            description=d.get("description", ""),
            options=dict(d["options"]),
            value_range=tuple(d["range"]) if d.get("range") else None,  # type: ignore[arg-type]
            option_cameras={k: list(v) for k, v in d.get("option_cameras", {}).items()},
            cameras=list(d.get("cameras", [])),
        )
    return defs


SETTING_DEFS: dict[int, SettingDef] = _build_defs()
SETTING_BY_NAME: dict[str, SettingDef] = {d.name: d for d in SETTING_DEFS.values()}


def normalize_model(model: str | None) -> str:
    return re.sub(r"\s+", " ", (model or "").strip()).casefold()


_KNOWN_MODELS = {normalize_model(c): c for c in CAMERAS}


def spec_model_name(model_name: str | None) -> str | None:
    """Map the camera-reported model name to the spec's camera name (None if unknown)."""
    return _KNOWN_MODELS.get(normalize_model(model_name))


def min_firmware_for(model_name: str | None) -> str | None:
    key = normalize_model(model_name)
    for name, fw in MIN_FIRMWARE.items():
        if normalize_model(name) == key:
            return fw
    return None


def _fw_tuple(version: str) -> tuple[int, ...]:
    """Extract the numeric x.y.z part of a firmware string like 'H24.01.01.10.00' or 'v01.10.00'."""
    nums = [int(n) for n in re.findall(r"\d+", version)]
    return tuple(nums[-3:]) if len(nums) >= 3 else tuple(nums)


def firmware_at_least(firmware: str, minimum: str) -> bool:
    return _fw_tuple(firmware) >= _fw_tuple(minimum)


def lookup(key: int | str) -> SettingDef:
    """Find a setting by numeric ID or parameter name (with or without the ``settings.`` prefix)."""
    if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
        sid = int(key)
        if sid not in SETTING_DEFS:
            raise InvalidSetting(f"Unknown setting id {sid} (not in Open GoPro spec tables)")
        return SETTING_DEFS[sid]
    name = key.removeprefix(PARAM_PREFIX)
    if name not in SETTING_BY_NAME:
        raise InvalidSetting(f"Unknown setting '{key}'")
    return SETTING_BY_NAME[name]


def is_supported_by_model(sdef: SettingDef, model: str | None) -> bool:
    """Spec-level support. Unknown model -> assume supported and let the camera decide."""
    spec_name = spec_model_name(model)
    if spec_name is None or not sdef.cameras:
        return True
    return spec_name in sdef.cameras


def options_for_model(sdef: SettingDef, model: str | None) -> dict[int, str]:
    spec_name = spec_model_name(model)
    if spec_name is None or not sdef.option_cameras:
        return dict(sdef.options)
    return {o: lbl for o, lbl in sdef.options.items() if spec_name in sdef.option_cameras.get(o, [spec_name])}


def _norm_label(text: str) -> str:
    t = text.strip().casefold().replace("_", " ")
    t = re.sub(r"\s+", "", t)
    return re.sub(r"\.0$", "", t)


def parse_option(sdef: SettingDef, value: Any) -> int:
    """Convert a parameter value (int option ID or str label) to an option ID."""
    if isinstance(value, bool):
        # Booleans are accepted for two-option on/off settings only.
        for opt, lbl in sdef.options.items():
            if _norm_label(lbl) == ("on" if value else "off"):
                return opt
        raise InvalidSetting(f"{sdef.param_name}: boolean value not meaningful for this setting")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        # An integer is an option ID; if it is not one but equals a label (e.g. 1080), use the label.
        if sdef.is_enumerated and value not in sdef.options:
            matches = [o for o, lbl in sdef.options.items() if _norm_label(lbl) == str(value)]
            if len(matches) == 1:
                return matches[0]
        return value
    if isinstance(value, str):
        wanted = _norm_label(value)
        matches = [o for o, lbl in sdef.options.items() if _norm_label(lbl) == wanted]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise InvalidSetting(
                f"{sdef.param_name}: label '{value}' is ambiguous (options {matches}); use the option ID"
            )
        if re.fullmatch(r"-?\d+", value.strip()):
            return int(value)
        labels = ", ".join(f"{o}={lbl!r}" for o, lbl in sdef.options.items())
        raise InvalidSetting(f"{sdef.param_name}: unknown option '{value}'. Valid: {labels}")
    raise InvalidSetting(f"{sdef.param_name}: unsupported parameter type {type(value).__name__}")


def validate_option(
    sdef: SettingDef,
    option: int,
    model: str | None,
    runtime_options: list[int] | None = None,
) -> None:
    """Raise InvalidSetting if ``option`` is not valid for this camera model (and current mode, if known)."""
    if not is_supported_by_model(sdef, model):
        raise InvalidSetting(f"{sdef.param_name} ({sdef.id}) is not supported on {model}")
    if sdef.is_enumerated:
        model_opts = options_for_model(sdef, model)
        if option not in sdef.options:
            raise InvalidSetting(
                f"{sdef.param_name}: option {option} does not exist in the spec",
                supported_options=sorted(model_opts),
            )
        if option not in model_opts:
            raise InvalidSetting(
                f"{sdef.param_name}: option {option} ({sdef.label(option)}) is not supported on {model}",
                supported_options=sorted(model_opts),
            )
    elif sdef.value_range is not None:
        lo, hi = sdef.value_range
        if not lo <= option <= hi:
            raise InvalidSetting(f"{sdef.param_name}: value {option} outside range [{lo}, {hi}]")
    if runtime_options and option not in runtime_options:
        raise InvalidSetting(
            f"{sdef.param_name}: option {option} ({sdef.label(option)}) not available in the current preset/mode",
            supported_options=sorted(runtime_options),
        )


def describe(sdef: SettingDef, model: str | None = None) -> str:
    """Human readable description for the ROS parameter descriptor."""
    if sdef.is_enumerated:
        opts = options_for_model(sdef, model)
        body = "; ".join(f"{o}={lbl}" for o, lbl in opts.items())
        return f"{sdef.title} (setting {sdef.id}). Options: {body}"
    rng = f" Range {sdef.value_range[0]}-{sdef.value_range[1]}." if sdef.value_range else ""
    return f"{sdef.title} (setting {sdef.id}). {sdef.description}{rng}"
