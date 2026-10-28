"""Shared recent-game growth signals for platform views and dashboard cards."""
import math

from . import charts, config


def text(row):
    label = row["label"]
    if row["kind"] == "rank":
        return f"刚起势：{label}，第 {int(row['then'])} 名到第 {int(row['now'])} 名"
    return f"刚起势：{label}，{row['then']:g} 到 {row['now']:g}"


def compute(entities):
    out = {}
    for source in sorted({s for s, _ in entities}):
        if not config.source_on(source):
            continue
        rows = []
        for observed in charts.rising(source)["rows"]:
            # Pending and stale observations stay visible in platform views, not as fresh alerts.
            if observed["state"] not in ("growth", "zero_baseline") or observed["stale"]:
                continue
            ratio = observed["growth"]
            metric = observed["metric"]
            label = config.RANK_BOARDS.get((source, metric), ("", metric))[1]
            row = dict(observed, label=label, now=observed["value"],
                       then=observed["previous"], total=1 + ratio if ratio is not None else None,
                       hot=min(.9, .3 + .2 * math.log2(1 + ratio)) if ratio is not None else .3)
            row["text"] = text(row)
            rows.append(row)
        if rows:
            out[source] = rows
    return out
