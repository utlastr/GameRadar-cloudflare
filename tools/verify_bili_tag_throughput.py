"""Measure the running collector through localhost without extra Bilibili requests."""
import argparse
import json
import time
import urllib.request
from datetime import datetime
from pathlib import Path


def snapshot():
    with urllib.request.urlopen("http://127.0.0.1:8765/api/bili-stream", timeout=60) as response:
        result = json.load(response)
    return {"at": time.time(), **result}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=180)
    parser.add_argument("--interval", type=float, default=15)
    parser.add_argument("--name", default="bili-tag-throughput")
    args = parser.parse_args()
    started = time.monotonic()
    samples = [snapshot()]
    while time.monotonic() - started < args.seconds:
        time.sleep(min(max(1, args.interval), max(0, args.seconds - (time.monotonic() - started))))
        samples.append(snapshot())
        current = samples[-1]
        print(json.dumps({k: current[k] for k in
                         ("at", "runtime_pid", "videos", "tagged", "pending_tags",
                          "new_last_minute", "tags_last_minute", "pause_seconds")}), flush=True)
    first, last = samples[0], samples[-1]
    seconds = max(0.001, last["at"] - first["at"])
    result = {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "seconds": seconds, "revision": last["collector_revision"],
        "runtime_pid": last["runtime_pid"],
        "discovered": last["videos"] - first["videos"],
        "completed": last["tagged"] - first["tagged"],
        "pending_change": last["pending_tags"] - first["pending_tags"],
        "discovered_per_minute": (last["videos"] - first["videos"]) * 60 / seconds,
        "completed_per_minute": (last["tagged"] - first["tagged"]) * 60 / seconds,
        "unavailable_change": last["unavailable"] - first["unavailable"],
        "statistics_scope": last.get("statistics_scope", "legacy_realtime_only"),
        "consistent_counts": all(
            s["videos"] == s["tagged"] + s["unavailable"] + s["pending_tags"] and
            s.get("queue", {}).get("total", s["pending_tags"]) == s["pending_tags"]
            for s in samples),
        "last_queue": last.get("queue"),
        "same_process": all(s["runtime_pid"] == first["runtime_pid"] for s in samples),
        "risk_pause_seen": any(s["pause_seconds"] or s["tag_pause_seconds"] for s in samples),
        "last_network": last["network"], "samples": samples,
    }
    root = Path(__file__).resolve().parents[1]
    target = root / "output" / (Path(args.name).name + ".json")
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "samples"}, indent=2), flush=True)


if __name__ == "__main__":
    main()
