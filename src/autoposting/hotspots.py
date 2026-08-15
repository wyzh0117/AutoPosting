from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class HotspotConfig:
    window_minutes: int = 60
    min_items: int = 6
    min_sources: int = 3
    baseline_days: int = 7
    stddev_multiplier: float = 3.0
    cooldown_hours: int = 6


@dataclass(frozen=True)
class Observation:
    source_id: str
    tier: str
    observed_at: datetime


@dataclass(frozen=True)
class HotspotDecision:
    topic: str
    triggered: bool
    evaluated_at: datetime
    window_start: datetime
    current_count: int
    independent_sources: int
    baseline_mean: float
    baseline_stddev: float
    statistical_threshold: float
    source_ids: tuple[str, ...]
    tiers: tuple[str, ...]
    reasons: tuple[str, ...]
    rule_version: str = "hotspot-v1"


class HotspotDetector:
    def __init__(self, config: HotspotConfig) -> None:
        self.config = config

    def evaluate(
        self,
        topic: str,
        observations: list[Observation],
        baseline_counts: list[int],
        now: datetime,
        last_triggered_at: datetime | None = None,
    ) -> HotspotDecision:
        window_start = now - timedelta(minutes=self.config.window_minutes)
        current = [item for item in observations if window_start <= item.observed_at <= now]
        source_ids = tuple(sorted({item.source_id for item in current}))
        tiers = tuple(sorted({item.tier for item in current}))
        baseline_mean = statistics.fmean(baseline_counts) if baseline_counts else 0.0
        baseline_stddev = statistics.pstdev(baseline_counts) if baseline_counts else 0.0
        threshold = baseline_mean + self.config.stddev_multiplier * baseline_stddev

        reasons: list[str] = []
        if len(current) < self.config.min_items:
            reasons.append("below_min_items")
        if len(source_ids) < self.config.min_sources:
            reasons.append("below_min_sources")
        if len(baseline_counts) < self.config.baseline_days:
            reasons.append("insufficient_baseline")
        elif len(current) <= threshold:
            reasons.append("below_statistical_threshold")

        has_s1 = any(item.tier == "S1" for item in current)
        corroborating_sources = {
            item.source_id for item in current if item.tier in {"S2", "S3"}
        }
        if not has_s1 and len(corroborating_sources) < 2:
            reasons.append("insufficient_trusted_sources")

        if last_triggered_at is not None:
            cooldown_end = last_triggered_at + timedelta(hours=self.config.cooldown_hours)
            if now < cooldown_end:
                reasons.append("cooldown_active")

        return HotspotDecision(
            topic=topic,
            triggered=not reasons,
            evaluated_at=now,
            window_start=window_start,
            current_count=len(current),
            independent_sources=len(source_ids),
            baseline_mean=baseline_mean,
            baseline_stddev=baseline_stddev,
            statistical_threshold=threshold,
            source_ids=source_ids,
            tiers=tiers,
            reasons=tuple(reasons),
        )