"""Time helpers. Minute 0 is D0 00:00; history has negative minutes."""
import datetime as dt


def minute_of(ts: str, d0: dt.date) -> int:
    t = dt.datetime.fromisoformat(ts)
    return (t.date() - d0).days * 1440 + t.hour * 60 + t.minute


def label(minute: int) -> str:
    day, mod = divmod(minute, 1440)
    return f"D{day} {mod // 60:02d}:{mod % 60:02d}"
