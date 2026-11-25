"""Universal diurnal time-of-day progression derived deterministically from tick and clock_interval."""

PHASES = (
    (1 / 9, "黎明"),
    (3 / 9, "上午"),
    (4 / 9, "正午"),
    (6 / 9, "下午"),
    (7 / 9, "黄昏"),
    (8 / 9, "夜晚"),
    (1.0, "深夜"),
)
NIGHT_PHASES = frozenset({"夜晚", "深夜"})


def diurnal(tick: int, clock_interval: int) -> tuple[int, str, bool]:
    """Return (day_number, time_of_day_label, is_night) for any positive clock_interval."""
    day = tick // clock_interval + 1
    if clock_interval <= 1:
        return day, "全日", False
    midpoint = ((tick % clock_interval) + 0.5) / clock_interval
    for bound, label in PHASES:
        if midpoint < bound:
            return day, label, label in NIGHT_PHASES
    return day, "深夜", True
