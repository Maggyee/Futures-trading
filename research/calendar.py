from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import ResearchError

TZ = ZoneInfo("Asia/Shanghai")
MINUTE = timedelta(minutes=1)


def stamp(value):
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)


def at(day, clock):
    return stamp(f"{day}T{clock}:00")


class Calendar:
    """Explicit historical dates; no weekday inference or night trading-day guessing."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.days = cfg["trading_days"]

    def previous(self, day):
        if day not in self.days or self.days.index(day) == 0:
            return None
        return self.days[self.days.index(day) - 1]

    def periods(self, day, contract, night=False):
        if day not in self.days:
            return []
        profile = contract["session_profile"]
        override = self.cfg.get("overrides", {}).get(day, {}).get(profile, {})
        schedule = override.get("sessions", self.cfg["profiles"][profile])
        result = [(at(day, start), at(day, end)) for start, end in schedule]
        if night and contract.get("night_sessions"):
            natural = self.cfg.get("night_dates", {}).get(day)
            if not natural:
                raise ResearchError(
                    f"{day}/{profile}: 缺少明确夜盘自然日映射 night_dates"
                )
            for period in contract["night_sessions"]:
                start = at(natural, period[0])
                end = at(natural, period[1])
                if end <= start:
                    end += timedelta(days=1)
                result.append((start, end))
        return sorted(result)

    def minutes(self, day, contract, night=False):
        return [
            start + i * MINUTE
            for start, end in self.periods(day, contract, night)
            for i in range(int((end - start).total_seconds() // 60))
        ]

    def bounds(self, day, contract):
        periods = self.periods(day, contract)
        return periods[0][0], periods[-1][1]

    def locate(self, dt, day, contract, night=False):
        for start, end in self.periods(day, contract, night):
            if start <= dt < end:
                return start, end
        return None

    def deadlines(self, day, contract, times):
        _, close = self.bounds(day, contract)
        override = (
            self.cfg.get("overrides", {})
            .get(day, {})
            .get(contract["session_profile"], {})
        )
        settings = {**times[contract["time_profile"]], **override.get("deadlines", {})}
        # Earlier close automatically clips strategy research deadlines.
        force = min(at(day, settings["force_close"]), close - MINUTE)
        cutoff = min(at(day, settings["entry_cutoff"]), force)
        return cutoff, force, close

    def infer_day(self, dt):
        day = dt.date().isoformat()
        if day in self.days and "08:00" <= dt.strftime("%H:%M") < "18:00":
            return day
        return None

    def in_break(self, dt, day, contract):
        periods = self.periods(day, contract)
        return any(end == dt and end != periods[-1][1] for _, end in periods)

    def holding_minutes(self, start, end, contract):
        return sum(
            start <= t < end for day in self.days for t in self.minutes(day, contract)
        )

    def validate(self):
        for day in self.days:
            date.fromisoformat(day)
            for profile in self.cfg["profiles"]:
                probe = {"session_profile": profile}
                periods = self.periods(day, probe)
                if (
                    not periods
                    or any(b <= a for a, b in periods)
                    or any(
                        periods[i][1] > periods[i + 1][0]
                        for i in range(len(periods) - 1)
                    )
                ):
                    raise ResearchError(f"{day}/{profile}: 交易时段重叠或为空")
