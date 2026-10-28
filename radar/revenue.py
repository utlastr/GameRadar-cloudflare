"""Daily acquisition cohort value, in CNY, with explicit conversion provenance.

No heat normalization, momentum bonus, acquisition costs, or development costs.
Missing coefficients remain missing. Scenario estimates are not settled cash.
"""
import copy
import json
import math
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import config, db, trend

MODEL_PATH = Path(__file__).with_name("revenue_model.json")


def _merge(base, overrides):
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def load_model():
    model = json.loads(MODEL_PATH.read_text(encoding="utf-8"))
    return _merge(model, db.kv_get("revenue_model", {}) or {})


def _number(value, low=0, high=None):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= low and (high is None or value <= high))


def _ratio(value):
    return _number(value, 0, 1)


def settle(iap, ads, rule, basis="gross"):
    """Apply settlement once; ads are publisher receipts before host share."""
    if basis == "developer_net":
        return iap + ads
    if basis != "gross":
        return None
    if iap and not _ratio(rule.get("iap_share")):
        return None
    if ads and not _ratio(rule.get("ad_share")):
        return None
    # Refunds/taxes apply to consumer purchases, not to advertising receipts.
    fields = ["withholding_rate", "settlement_fee_rate"]
    if iap:
        fields += ["refund_rate", "indirect_tax_rate", "payment_channel_rate"]
    if any(not _ratio(rule.get(k)) for k in fields):
        return None
    if ads and not _number(rule.get("invoice_adjustment_factor"), .000001):
        return None
    net_iap = (iap * (1 - rule["refund_rate"]) / (1 + rule["indirect_tax_rate"])
               * (1 - rule["payment_channel_rate"])) if iap else 0
    net = (net_iap * (rule.get("iap_share") or 0)
           + ads * (rule.get("ad_share") or 0) * rule.get("invoice_adjustment_factor", 1))
    return net * (1 - rule["withholding_rate"]) * (1 - rule["settlement_fee_rate"])


def _flow(source, entity, rule, series, ranks, now):
    metric = rule.get("metric")
    if rule.get("kind") == "rank":
        observed = ranks.get((source, entity, metric))
        if not observed:
            return None, "缺当前同榜单名次"
        ts, rank = observed
        if not rule.get("min_rank", 1) <= rank <= rule.get("max_rank", math.inf):
            return None, "不在指定新品榜名次区间"
        anchor, exponent = rule.get("anchor_daily_users"), rule.get("exponent")
        if not _number(anchor) or not _number(exponent, .000001) or not _number(rule.get("anchor_rank"), 1):
            return None, "缺名次→日新增用户锚点或曲线指数"
        users = anchor * (rule["anchor_rank"] / rank) ** exponent
        return {"daily_users": users, "metric": metric, "raw": rank, "sampled_at": ts,
                "conversion": rule, "status": "scenario"}, None
    p = trend.counter_profile(series.get((source, entity, metric), []), now)
    if p is None:
        return None, "缺新鲜的连续计数采样，无法计算日增量"
    multiplier = rule.get("multiplier")
    if not _number(multiplier, .000001):
        return None, "缺原始指标→首次用户的校准系数"
    pts = series[(source, entity, metric)]
    return {"daily_users": p["value"] * multiplier, "metric": metric, "raw": p["value"],
            "sampled_at": pts[-1][0], "observed_days": p["recent_days"],
            "conversion": rule, "status": rule.get("status", "scenario")}, None


def _mobile_value(source, attrs, model):
    genre, mode = attrs.get("genre"), attrs.get("monetization")
    mix = attrs.get("country_mix")
    if mode not in ("iap", "iaa", "hybrid") or not genre or not isinstance(mix, dict) or not mix:
        return None, "缺国家用户占比、品类或变现模式"
    if any(not _ratio(w) for w in mix.values()) or not math.isclose(sum(mix.values()), 1, abs_tol=1e-6):
        return None, "国家用户占比必须合计为 1"
    total, refs = 0.0, []
    for country, weight in mix.items():
        if weight == 0:
            continue
        p = next((p for p in model.get("profiles", []) if p.get("country") == country
                  and p.get("genre") == genre and p.get("monetization") == mode
                  and p.get("horizon_days") == model["horizon_days"]), None)
        if not p:
            return None, f"缺 {country}/{genre}/{mode} 的 D{model['horizon_days']} 每新增用户收入"
        if not p.get("source"):
            return None, "收入参数缺来源或情景说明"
        iap = p.get("iap_cny", 0) if mode in ("iap", "hybrid") else 0
        ads = p.get("iaa_cny", 0) if mode in ("iaa", "hybrid") else 0
        if not _number(iap) or not _number(ads):
            return None, "每用户收入参数无效"
        if mode == "hybrid" and not p.get("joint_sample"):
            return None, "混合变现需同批用户样本，不能直接相加不同报告均值"
        contract = model.get("contracts", {}).get(attrs.get("contract"), {})
        platform = contract.get("platform")
        if contract and platform != source and not (platform == "apple" and source in ("appcn", "appus")):
            return None, "结算合同与平台不匹配"
        rule = {**model.get("settlements", {}).get(source, {}), **contract, **attrs.get("settlement", {})}
        value = settle(iap, ads, rule, p.get("basis", "gross"))
        if value is None:
            return None, "缺收入净额口径或平台分成/退款/税费结算参数"
        total += weight * value
        refs.append({"country": country, "weight": weight, "profile": p, "settlement": rule})
    return {"net_per_user_cny": total, "profiles": refs, "assumptions": []}, None


def estimate_member(member, ent, model, series, ranks, now):
    source, entity = member.split(":", 1)
    attrs = (ent.get("extra") or {}).get("revenue") or {}
    conversion = {**model.get("conversions", {}).get(source, {}), **attrs.get("conversion", {})}
    result = {"member": member, "source": source, "daily_users": None,
              "net_per_user_cny": None, "daily_cny": None, "status": "missing"}
    if source == "steam" and (ent.get("extra") or {}).get("is_free"):
        result["reason"] = "免费游戏缺首次用户和内购收入模型"
        return result
    if not conversion.get("metric"):
        result["reason"] = model.get("platform_notes", {}).get(source, "缺首次用户换算模型")
        return result
    flow, error = _flow(source, entity, conversion, series, ranks, now)
    if error:
        result["reason"] = error
        return result
    result.update(flow)
    result["status"] = "missing"
    if source == "steam":
        extra = ent.get("extra") or {}
        price = extra.get("price_usd")
        if extra.get("is_free") or not _number(price, .000001):
            result["reason"] = "免费或缺价格；评论不能推导内购收入"
            return result
        rule = {**model["steam"], **attrs.get("settlement", {})}
        factor = rule.get("average_price_factor")
        if not _number(factor) or not _number(model.get("usd_cny"), .000001):
            result["reason"] = "缺实收均价调整系数或美元汇率"
            return result
        gross = price * factor * model["usd_cny"]
        net = settle(gross, 0, {**rule, "iap_share": rule.get("developer_share")})
        value = {"net_per_user_cny": net, "price_usd": price, "settlement": rule,
                 "assumptions": list(rule.get("assumptions", [])),
                 "scope": "paid_base_game_only",
                 "reference_source": "https://partner.steamgames.com/doc/finance/payments_salesreporting"}
        error = "缺平台分成或结算扣减参数" if net is None else None
    else:
        value, error = _mobile_value(source, attrs, model)
    if error:
        result["reason"] = error
        return result
    result.update(value)
    result.update(daily_cny=flow["daily_users"] * value["net_per_user_cny"],
                  status="scenario", horizon_days=model["horizon_days"])
    return result


def rank_key(result):
    value = result.get("daily_cny")
    return (value is None, -(value or 0))


def aggregate(rows, horizon=90):
    # Different storefronts may cover the same users. Do not sum without a
    # disjoint acquisition cohort. Select one complete scenario per game.
    usable = [r for r in rows if r.get("daily_cny") is not None]
    best = min(usable, key=rank_key) if usable else None
    return {"daily_cny": best["daily_cny"] if best else None,
            "daily_users": best["daily_users"] if best else None,
            "net_per_user_cny": best["net_per_user_cny"] if best else None,
            "status": "scenario" if best else "missing",
            "horizon_days": horizon, "currency": "CNY",
            "selected_member": best["member"] if best else None,
            "aggregation": "best_single_channel_no_sum",
            "partial": bool(best) and len(usable) < len(rows),
            "channels": rows}


def score_groups(groups, entities, now=None, model=None):
    now = int(time.time() if now is None else now)
    model = load_model() if model is None else model
    series, ranks = defaultdict(list), {}
    metrics = {"reviews", "downloads"}
    metrics.update(r.get("metric") for r in model.get("conversions", {}).values() if r.get("metric"))
    metrics.update(((e.get("extra") or {}).get("revenue") or {}).get("conversion", {}).get("metric")
                   for e in entities.values())
    metrics.discard(None)
    with db.session() as c:
        placeholders = ",".join("?" for _ in metrics)
        rows = c.execute(f"SELECT source,entity,metric,ts,value FROM metrics "
                         f"WHERE metric IN ({placeholders}) AND ts BETWEEN ? AND ? ORDER BY ts",
                         (*sorted(metrics), now - 12 * trend.DAY, now)).fetchall()
    latest_rank = {}
    for src, eid, metric, ts, value in rows:
        if not config.source_on(src) or value is None:
            continue
        if metric.startswith("rank_"):
            latest_rank[(src, metric)] = max(latest_rank.get((src, metric), 0), ts)
            if _number(value, 1) and now - ts <= trend.FRESH:
                ranks[(src, eid, metric)] = (ts, value)
        else:
            series[(src, eid, metric)].append((ts, value))
    # An absent game in the newest chart must not retain its old rank.
    ranks = {k: v for k, v in ranks.items() if v[0] == latest_rank[k[0], k[2]]}
    out, counts, reasons = {}, Counter(), Counter()
    for gid, members in groups.items():
        channels, seen = [], set()
        for member in members:
            src, eid = member.split(":", 1)
            if src not in config.DISCOVERY_SOURCES or not config.source_on(src):
                continue
            key = ("tap" if src in ("taptap", "tapmaker") else src, eid)
            # Prefer the duplicate with a complete value, not member ordering.
            row = estimate_member(member, entities.get((src, eid), {}), model, series, ranks, now)
            if key in seen:
                previous = next(r for r in channels if r["_key"] == key)
                if rank_key(row) < rank_key(previous):
                    channels.remove(previous)
                else:
                    continue
            seen.add(key)
            channels.append({**row, "_key": key})
        for row in channels:
            row.pop("_key")
            counts[row["source"]] += row["daily_cny"] is not None
            if row.get("reason"):
                reasons[row["reason"]] += 1
        out[gid] = aggregate(channels, model["horizon_days"])
    return out, {"version": model["version"], "horizon_days": model["horizon_days"],
                 "currency": "CNY", "fx": {"usd_cny": model["usd_cny"], "date": model["fx_date"],
                                         "source": model["fx_source"]},
                 "scored_channels": dict(counts), "missing_reasons": dict(reasons),
                 "platform_notes": model.get("platform_notes", {}),
                 "benchmark_source": model.get("benchmark_source"),
                 "mobile_benchmarks": model.get("mobile_benchmarks", []),
                 "benchmark_note": model.get("benchmark_note"),
                 "meaning": "每日新增用户在未来指定周期内带来的开发者净收入情景；不是当天到账"}


def visible_groups(keys, scores, limit=400):
    return sorted((g for g in keys if scores.get(g, {}).get("daily_cny") is not None),
                  key=lambda g: (*rank_key(scores[g]), g))[:limit]
