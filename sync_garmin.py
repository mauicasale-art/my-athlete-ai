#!/usr/bin/env python3
"""
Garmin -> files sync script (compatibile con garminconnect 0.3.x).
Usage:
  python sync_garmin.py --login
  python sync_garmin.py --days 3 --dry-run
  python sync_garmin.py --days 3 --sink files --out ./garmin
"""

import argparse
import base64
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

TOKEN_DIR = Path.home() / ".garminconnect"


# ── auth ───────────────────────────────────────────────────────────────────────

def do_login():
    email = os.environ.get("GARMIN_EMAIL", "")
    password = os.environ.get("GARMIN_PASSWORD", "")
    if not email or not password:
        sys.exit("Imposta GARMIN_EMAIL e GARMIN_PASSWORD prima di --login.")

    from garminconnect import Garmin

    garmin = Garmin(
        email=email,
        password=password,
        prompt_mfa=lambda: input("Codice MFA Garmin: ").strip(),
    )
    garmin.login(str(TOKEN_DIR))
    print(f"\nLogin OK. Token salvati in: {TOKEN_DIR}")

    # Leggi i file del token e codificali in base64 per GitHub Actions
    token_files = list(TOKEN_DIR.glob("*"))
    bundle = {}
    for f in token_files:
        bundle[f.name] = f.read_text(encoding="utf-8")

    token_b64 = base64.b64encode(json.dumps(bundle).encode()).decode()
    print("\n=== TOKEN BASE64 (copialo in GARMIN_TOKEN_B64 su GitHub) ===\n")
    print(token_b64)
    print("\n=== FINE TOKEN ===\n")


def get_client():
    from garminconnect import Garmin

    # Caso GitHub Actions: token passato come variabile d'ambiente
    token_b64 = os.environ.get("GARMIN_TOKEN_B64", "")
    if token_b64:
        bundle = json.loads(base64.b64decode(token_b64).decode())
        tmp = Path(tempfile.mkdtemp())
        for name, content in bundle.items():
            (tmp / name).write_text(content, encoding="utf-8")
        garmin = Garmin()
        garmin.login(str(tmp))
        return garmin

    # Caso locale: token salvati da --login
    if TOKEN_DIR.exists() and any(TOKEN_DIR.iterdir()):
        garmin = Garmin()
        garmin.login(str(TOKEN_DIR))
        return garmin

    sys.exit("Nessun token trovato. Esegui prima --login oppure imposta GARMIN_TOKEN_B64.")


# ── fetch ──────────────────────────────────────────────────────────────────────

def safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


def fetch_wellness(client, day: date) -> dict:
    ds = day.isoformat()
    out = {"date": ds}

    stats = safe(client.get_user_summary, ds)
    if stats:
        out["resting_hr"] = stats.get("restingHeartRate")
        out["steps"] = stats.get("totalSteps")
        out["stress_avg"] = stats.get("averageStressLevel")
        out["body_battery_start"] = stats.get("bodyBatteryLowestValue")
        out["body_battery_end"] = stats.get("bodyBatteryHighestValue")

    sleep = safe(client.get_sleep_data, ds)
    if sleep:
        daily = sleep.get("dailySleepDTO", {})
        out["sleep_seconds"] = daily.get("sleepTimeSeconds")
        scores = daily.get("sleepScores")
        if isinstance(scores, dict):
            out["sleep_score"] = scores.get("overall", {}).get("value")

    hrv = safe(client.get_hrv_data, ds)
    if hrv:
        summary = hrv.get("hrvSummary", {})
        out["hrv_overnight"] = summary.get("overnight")

    readiness = safe(client.get_training_readiness, ds)
    if isinstance(readiness, list) and readiness:
        out["training_readiness"] = readiness[0].get("score")
    elif isinstance(readiness, dict):
        out["training_readiness"] = readiness.get("score")

    return out


def fetch_activities(client, day: date) -> list:
    ds = day.isoformat()
    result = safe(client.get_activities_by_date, ds, ds)
    return result or []


# ── formattazione ──────────────────────────────────────────────────────────────

def wellness_to_md(w: dict) -> str:
    lines = [f"# Garmin wellness {w['date']}"]
    if w.get("resting_hr"):
        lines.append(f"- Resting HR: {w['resting_hr']} bpm")
    if w.get("hrv_overnight"):
        lines.append(f"- HRV (overnight): {w['hrv_overnight']} ms")
    secs = w.get("sleep_seconds")
    if secs:
        hours = round(secs / 3600, 1)
        line = f"- Sleep: {hours} h"
        if w.get("sleep_score"):
            line += f" (score {w['sleep_score']})"
        lines.append(line)
    bs = w.get("body_battery_start")
    be = w.get("body_battery_end")
    if bs is not None and be is not None:
        lines.append(f"- Body battery: {bs} -> {be}")
    if w.get("stress_avg"):
        lines.append(f"- Stress (avg): {w['stress_avg']}")
    if w.get("steps"):
        lines.append(f"- Steps: {w['steps']}")
    if w.get("training_readiness"):
        lines.append(f"- Training readiness: {w['training_readiness']}")
    return "\n".join(lines) + "\n"


def activity_to_md(a: dict, day: date) -> tuple:
    name = a.get("activityName", "Activity").replace(" ", "-").replace("/", "-")
    act_type = a.get("activityType", {}).get("typeKey", "unknown")
    start = a.get("startTimeLocal", day.isoformat())[:10]
    filename = f"{start}-{name}.md"

    distance_km = round((a.get("distance") or 0) / 1000, 2)
    duration_min = round((a.get("duration") or 0) / 60, 1)

    lines = [
        f"# {a.get('activityName', 'Activity')} — {start}",
        f"- Type: {act_type}",
    ]
    if distance_km:
        lines.append(f"- Distance: {distance_km} km")
    if duration_min:
        lines.append(f"- Duration: {duration_min} min")
    if a.get("averageHR"):
        lines.append(f"- Avg HR: {a['averageHR']} bpm")
    if a.get("maxHR"):
        lines.append(f"- Max HR: {a['maxHR']} bpm")
    if a.get("calories"):
        lines.append(f"- Calories: {a['calories']} kcal")
    if a.get("elevationGain"):
        lines.append(f"- Elevation gain: {a['elevationGain']} m")
    return filename, "\n".join(lines) + "\n"


# ── sink files ─────────────────────────────────────────────────────────────────

def sink_files(all_wellness, all_activities, out_dir: Path, dry_run: bool):
    daily_dir = out_dir / "daily"
    act_dir = out_dir / "activities"
    if not dry_run:
        daily_dir.mkdir(parents=True, exist_ok=True)
        act_dir.mkdir(parents=True, exist_ok=True)

    for w in all_wellness:
        md = wellness_to_md(w)
        path = daily_dir / f"{w['date']}.md"
        print(f"  [wellness] {path}")
        if dry_run:
            print(md)
        else:
            path.write_text(md, encoding="utf-8")

    for day, activities in all_activities:
        for a in activities:
            fname, md = activity_to_md(a, day)
            path = act_dir / fname
            print(f"  [activity] {path}")
            if dry_run:
                print(md)
            else:
                path.write_text(md, encoding="utf-8")

    if not dry_run:
        data = {
            "wellness": all_wellness,
            "activities": [{"date": str(d), "list": acts} for d, acts in all_activities],
        }
        (out_dir / "data.json").write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8"
        )
        print(f"  [json] {out_dir / 'data.json'}")


def sink_supabase(all_wellness, all_activities, dry_run: bool):
    import urllib.request

    url = os.environ.get("GARMIN_INGEST_URL", "")
    secret = os.environ.get("GARMIN_INGEST_SECRET", "")
    if not url:
        sys.exit("Imposta GARMIN_INGEST_URL per usare --sink supabase.")

    payload = json.dumps({
        "wellness": all_wellness,
        "activities": [{"date": str(d), "list": acts} for d, acts in all_activities],
    }).encode()

    print(f"  [supabase] POST -> {url}")
    if dry_run:
        return
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {secret}"},
    )
    with urllib.request.urlopen(req) as resp:
        print(f"  [supabase] {resp.status} {resp.reason}")


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--login", action="store_true")
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--sink", choices=["files", "supabase"], default="files")
    parser.add_argument("--out", default="./garmin")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.login:
        do_login()
        return

    client = get_client()
    today = date.today()
    all_wellness = []
    all_activities = []

    for i in range(args.days):
        day = today - timedelta(days=i)
        print(f"Fetching {day} ...")
        w = fetch_wellness(client, day)
        all_wellness.append(w)
        acts = fetch_activities(client, day)
        all_activities.append((day, acts))
        filled = [k for k, v in w.items() if v is not None and k != "date"]
        print(f"  wellness: {filled}")
        print(f"  attività: {len(acts)}")

    if args.sink == "files":
        sink_files(all_wellness, all_activities, Path(args.out), args.dry_run)
    else:
        sink_supabase(all_wellness, all_activities, args.dry_run)

    print("Fatto.")


if __name__ == "__main__":
    main()
