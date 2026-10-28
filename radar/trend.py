"""Observed counter increments, smoothing, and bounded momentum."""
import math

DAY = 86400
HISTORY_DAYS = 10
MIN_SPAN = 3600
MAX_GAP = 36 * 3600
FRESH = 32 * 3600
TREND_WEIGHT = 0.2
WEIGHTS = (0.5, 0.3, 0.2)


def daily_rates(points, now, published=None, cumulative=True):
    """Newest day first: counter increments or time-weighted instantaneous levels."""
    clean = {}
    for ts, value in points:
        if value is not None and math.isfinite(value) and value >= 0 and ts <= now:
            clean[ts] = float(value)
    pts = sorted(clean.items())
    if not pts or now - pts[-1][0] > FRESH:
        return None
    if published and 0 < published < pts[0][0] and pts[0][0] - published <= MAX_GAP:
        pts.insert(0, (published, 0.0))
    if len(pts) < 2 or pts[-1][0] - pts[0][0] < MIN_SPAN:
        return None
    end = pts[-1][0]
    days = []
    for i in range(HISTORY_DAYS):
        lo, hi = end - (i + 1) * DAY, end - i * DAY
        if published and hi <= published:
            days.append(None)
            continue
        start = max(lo, published or lo)
        covered, delta, invalid = 0, 0.0, False
        for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
            span = min(t1, hi) - max(t0, start)
            if span <= 0:
                continue
            if t1 - t0 > MAX_GAP or (cumulative and v1 < v0):
                invalid = True
                continue
            covered += span
            if cumulative:
                delta += (v1 - v0) * span / (t1 - t0)
            else:
                left, right = max(t0, start), min(t1, hi)
                mean = v0 + (v1 - v0) * (left + right - 2 * t0) / (2 * (t1 - t0))
                delta += mean * span
        # Only the first observed, current day may use a partial interval.
        partial = i == 0 and start < pts[0][0] < hi and covered == hi - pts[0][0]
        valid = covered >= MIN_SPAN and (covered == hi - start or partial)
        scale = DAY if cumulative else 1
        days.append(delta * scale / covered if valid and not invalid else None)
    return days


def profile(days):
    """A = recent three days, B = preceding seven observed days; no zero filling."""
    if not days or days[0] is None:
        return None
    recent = [(w, d) for w, d in zip(WEIGHTS, days[:3]) if d is not None]
    base = sum(w * d for w, d in recent) / sum(w for w, _ in recent)
    a_days = [d for d in days[:3] if d is not None]
    b_days = [d for d in days[3:10] if d is not None]
    a = sum(a_days) / len(a_days)
    # A trend comparison needs complete recent days, not a single launch spike.
    b = sum(b_days) / len(b_days) if len(a_days) == 3 and b_days else None
    trend = (a - b) / (a + b) if b is not None and a + b else 0.0
    factor = 1.0 + TREND_WEIGHT * trend
    return {"value": base, "recent": a, "baseline": b, "trend": trend,
            "trend_known": b is not None, "trend_factor": factor,
            "adjusted": base * factor, "days": list(days),
            "recent_days": len(a_days), "baseline_days": len(b_days)}


def counter_profile(points, now, published=None):
    return profile(daily_rates(points, now, published))


def level_profile(points, now):
    return profile(daily_rates(points, now, cumulative=False))
