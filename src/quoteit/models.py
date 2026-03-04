from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class UsageWindow:
    utilization: float          # 0–100, percent *used*
    resets_at: Optional[str] = None


@dataclass
class ExtraUsage:
    is_enabled:    bool
    monthly_limit: float        # cents → dollars
    used_credits:  float        # cents → dollars
    utilization:   float
    currency:      str


@dataclass
class UsageResult:
    source:               str                   = "unknown"
    plan:                 Optional[str]         = None
    email:                Optional[str]         = None
    five_hour:            Optional[UsageWindow] = None
    seven_day:            Optional[UsageWindow] = None
    seven_day_opus:       Optional[UsageWindow] = None
    seven_day_sonnet:     Optional[UsageWindow] = None
    seven_day_oauth_apps: Optional[UsageWindow] = None
    iguana_necktie:       Optional[UsageWindow] = None
    extra_usage:          Optional[ExtraUsage]  = None

    def to_dict(self) -> dict:
        def window_dict(w: Optional[UsageWindow]) -> Optional[dict]:
            if w is None:
                return None
            return {"utilization": w.utilization, "resets_at": w.resets_at}

        d: dict = {"source": self.source, "plan": self.plan, "email": self.email}
        d["five_hour"]            = window_dict(self.five_hour)
        d["seven_day"]            = window_dict(self.seven_day)
        d["seven_day_opus"]       = window_dict(self.seven_day_opus)
        d["seven_day_sonnet"]     = window_dict(self.seven_day_sonnet)
        d["seven_day_oauth_apps"] = window_dict(self.seven_day_oauth_apps)
        d["iguana_necktie"]       = window_dict(self.iguana_necktie)
        if self.extra_usage:
            eu = self.extra_usage
            d["extra_usage"] = {
                "is_enabled":    eu.is_enabled,
                "monthly_limit": eu.monthly_limit,
                "used_credits":  eu.used_credits,
                "utilization":   eu.utilization,
                "currency":      eu.currency,
            }
        else:
            d["extra_usage"] = None
        return d

    def print_summary(self, title: str = "Usage") -> None:
        sep = "─" * 50
        print(f"\n{sep}")
        print(f"  {title}  (source: {self.source})")
        print(sep)
        if self.plan:
            print(f"  Plan    : {self.plan}")
        if self.email:
            print(f"  Account : {self.email}")

        def fmt_window(label: str, w: Optional[UsageWindow]) -> None:
            if w is None:
                return
            bar_filled = int(w.utilization / 5)      # 20-char bar
            bar = "█" * bar_filled + "░" * (20 - bar_filled)
            resets = f"  resets {w.resets_at}" if w.resets_at else ""
            print(f"  {label:<18} [{bar}] {w.utilization:5.1f}%{resets}")

        print()
        fmt_window("5-hour / session",  self.five_hour)
        fmt_window("7-day (all)",        self.seven_day)
        fmt_window("7-day Opus",         self.seven_day_opus)
        fmt_window("7-day Sonnet",       self.seven_day_sonnet)
        fmt_window("7-day OAuth apps",   self.seven_day_oauth_apps)
        fmt_window("iguana_necktie",     self.iguana_necktie)

        if self.extra_usage and self.extra_usage.is_enabled:
            eu = self.extra_usage
            print(
                f"\n  Extra usage : {eu.used_credits:.2f} / "
                f"{eu.monthly_limit:.2f} {eu.currency} "
                f"({eu.utilization:.1f}%)"
            )
        print()
