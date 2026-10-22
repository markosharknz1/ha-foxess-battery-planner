"""Pure FoxESS Mode Schedule builders (v3 scheduler group shape).

Groups use the nested ``extraParam`` form the v3 endpoints return and
accept. Times are minutes since midnight; an end of 1440 means "to the
end of the day" and maps to the API's inclusive 23:59.

The FoxESS schedule is replaced wholesale on every write, so the planner
keeps the user's own groups (the *baseline*) and only carves the charge
window out of them.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

DAY = 1440


class ScheduleError(Exception):
    """The schedule cannot be built safely; nothing should be written."""


@dataclass(frozen=True)
class ScheduleParams:
    min_soc_on_grid: int = 10
    charge_power_w: int = 0  # 0 = leave fdPwr out of the charge group
    supports_max_soc: bool = True
    max_groups: int = 8


def group_span(group: dict) -> tuple[int, int]:
    """(start, end_exclusive) in minutes."""
    start = int(group["startHour"]) * 60 + int(group["startMinute"])
    end = int(group["endHour"]) * 60 + int(group["endMinute"]) + 1
    return start, end


def is_remain(group: dict) -> bool:
    """The all-day group that acts as the background mode."""
    if group.get("isRemainMode") is True:
        return True
    try:
        return group_span(group) == (0, DAY)
    except (KeyError, TypeError, ValueError):
        return False


def _set_span(group: dict, start: int, end: int) -> dict:
    out = copy.deepcopy(group)
    last = end - 1
    out["startHour"], out["startMinute"] = divmod(start, 60)
    out["endHour"], out["endMinute"] = divmod(last, 60)
    out["isRemainMode"] = (start, end) == (0, DAY)
    return out


def make_group(
    mode: str,
    start: int,
    end: int,
    params: ScheduleParams,
    max_soc: int | None = None,
) -> dict:
    if not 0 <= start < end <= DAY:
        raise ScheduleError(f"Invalid time span {start}-{end}")
    extra: dict = {"minSocOnGrid": int(params.min_soc_on_grid)}
    if mode == "ForceCharge":
        target = 100 if max_soc is None else int(max_soc)
        if params.supports_max_soc:
            extra["maxSoc"] = target
        extra["fdSoc"] = target
        if params.charge_power_w > 0:
            extra["fdPwr"] = int(params.charge_power_w)
    return _set_span({"workMode": mode, "extraParam": extra}, start, end)


def normalize_baseline(api_groups) -> list[dict]:
    """Keep only well-formed groups from a ``scheduler/get`` response."""
    out: list[dict] = []
    for group in api_groups or []:
        if not isinstance(group, dict) or not group.get("workMode"):
            continue
        try:
            start, end = group_span(group)
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= start < end <= DAY:
            continue
        clean = copy.deepcopy(group)
        if not isinstance(clean.get("extraParam"), dict):
            clean["extraParam"] = {}
        clean["isRemainMode"] = (start, end) == (0, DAY)
        out.append(clean)
    return out


def _subtract(group: dict, cut_start: int, cut_end: int) -> list[dict]:
    """Pieces of a (non-remain) group left after removing [cut_start, cut_end)."""
    start, end = group_span(group)
    if end <= cut_start or start >= cut_end:
        return [copy.deepcopy(group)]
    pieces = []
    if start < cut_start:
        pieces.append(_set_span(group, start, cut_start))
    if end > cut_end:
        pieces.append(_set_span(group, cut_end, end))
    return pieces


def check_windows(windows) -> list[tuple[int, int]]:
    """Sorted, validated (start, end) windows: inside the day, non-empty, not overlapping."""
    cleaned = sorted((int(s), int(e)) for s, e in windows)
    if not cleaned:
        raise ScheduleError("No charge window configured")
    prev_end = 0
    for start, end in cleaned:
        if not 0 <= start < end <= DAY:
            raise ScheduleError(f"Invalid charge window {start}-{end}")
        if start < prev_end:
            raise ScheduleError("Charge windows overlap")
        prev_end = end
    return cleaned


def build_charge_schedule(
    baseline: list[dict],
    windows,
    max_soc: int,
    params: ScheduleParams,
) -> list[dict]:
    """Baseline with a ForceCharge group carved in for each window."""
    windows = check_windows(windows)

    remain: list[dict] = []
    kept: list[dict] = []
    for group in baseline:
        if is_remain(group):
            remain.append(copy.deepcopy(group))
            continue
        pieces = [group]
        for w_start, w_end in windows:
            pieces = [p for piece in pieces for p in _subtract(piece, w_start, w_end)]
        kept.extend(pieces)

    if not remain:
        remain = [make_group("SelfUse", 0, DAY, params)]
    for w_start, w_end in windows:
        kept.append(make_group("ForceCharge", w_start, w_end, params, max_soc))
    kept.sort(key=lambda g: group_span(g)[0])
    groups = remain[:1] + kept
    if len(groups) > params.max_groups:
        raise ScheduleError(
            f"{len(groups)} schedule groups needed but the inverter allows "
            f"{params.max_groups}; free one in the FoxESS app"
        )
    return groups


def build_restore_schedule(
    baseline: list[dict] | None, params: ScheduleParams
) -> list[dict]:
    """What to write back at the end: the user's baseline, or all-day Self Use."""
    groups = copy.deepcopy(baseline) if baseline else []
    if not any(is_remain(g) for g in groups):
        groups.insert(0, make_group("SelfUse", 0, DAY, params))
    return groups


def has_charge_windows(groups, windows) -> bool:
    """True if every window is fully covered by a ForceCharge group."""
    spans = []
    for group in groups or []:
        if group.get("workMode") != "ForceCharge":
            continue
        try:
            spans.append(group_span(group))
        except (KeyError, TypeError, ValueError):
            continue
    return all(any(s <= w_start and e >= w_end for s, e in spans) for w_start, w_end in windows)


def strip_charge_windows(groups, windows) -> list[dict]:
    """Remove ForceCharge groups that exactly match one of our windows (recovers
    a clean baseline if the live schedule still holds our own charge blocks)."""
    ours = {(int(s), int(e)) for s, e in windows}
    out = []
    for group in normalize_baseline(groups):
        if group.get("workMode") == "ForceCharge" and group_span(group) in ours:
            continue
        out.append(group)
    return out
