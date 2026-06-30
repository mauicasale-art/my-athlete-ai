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


def normalize_activity(a: dict) -> dict | None:
    """Converte un'attività Garmin nel formato usato dalla dashboard."""
    type_key = (a.get("activityType") or {}).get("typeKey", "")
    sport_key = (a.get("sportType") or {}).get("sportTypeKey", "")
    if "running" not in type_key.lower() and "running" not in sport_key.lower():
        return None
    half_cad = a.get("averageCadence") or 0
    return {
        "id": str(a.get("activityId", "")),
        "name": a.get("activityName") or "Corsa",
        "sport_type": "Run",
        "start_local": (a.get("startTimeLocal") or "").replace(" ", "T"),
        "distance": round(float(a.get("distance") or 0), 1),
        "moving_time": int(a.get("movingDuration") or a.get("duration") or 0),
        "avg_speed": round(float(a.get("averageSpeed") or 0), 4),
        "avg_hr": round(float(a.get("averageHR") or 0), 1) or None,
        "max_hr": int(a.get("maxHR") or 0) or None,
        "avg_cadence": round(half_cad * 2, 1) if half_cad else None,
        "total_elevation_gain": round(float(a.get("elevationGain") or 0), 1),
        "calories": int(a.get("calories") or 0) or None,
    }


def parse_fit_biomechanics(fit_bytes: bytes) -> list:
    """Parsa FIT e ritorna lap di sforzo con GCT, cadenza, oscillazione."""
    import io
    try:
        import fitdecode
    except ImportError:
        print("  [warn] fitdecode non installato: pip install fitdecode")
        return []
    laps = []
    with fitdecode.FitReader(io.BytesIO(fit_bytes)) as fit:
        for frame in fit:
            if not (isinstance(frame, fitdecode.FitDataMessage) and frame.name == 'lap'):
                continue
            d = {f.name: f.value for f in frame.fields if f.value is not None}
            cad = d.get('avg_running_cadence') or 0
            dist = d.get('total_distance') or 0
            hr = d.get('avg_heart_rate') or 0
            # Solo lap di sforzo (running, non recovery camminate)
            if cad < 70 or dist < 200 or hr < 145:
                continue
            dur = d.get('total_timer_time') or 0
            pace_s = round(dur / dist * 1000) if dist else None
            laps.append({
                'dist_m': round(dist),
                'pace_s': pace_s,
                'gct_ms': round(d['avg_stance_time'], 1) if d.get('avg_stance_time') else None,
                'cadence_spm': round(cad * 2),
                'osc_mm': round(d['avg_vertical_oscillation'], 1) if d.get('avg_vertical_oscillation') else None,
                'step_mm': round(d['avg_step_length']) if d.get('avg_step_length') else None,
                'hr': round(hr),
            })
    return laps


def sync_biomechanics(client, acts_path: Path, biomech_path: Path, dry_run: bool):
    """Scarica FIT per ogni sessione in garmin_activities.json e aggiorna biomech.json."""
    import io, zipfile

    acts = json.loads(acts_path.read_text(encoding='utf-8'))
    biomech = json.loads(biomech_path.read_text(encoding='utf-8'))
    existing_ids = {e['id'] for e in biomech.get('rep_biomechanics', [])}
    rep_bio = list(biomech.get('rep_biomechanics', []))

    for act in acts:
        if act['id'] in existing_ids:
            continue
        if act.get('distance', 0) < 3000:
            continue
        label = act['name'].split(' - ')[-1] if ' - ' in act['name'] else act['name']
        print(f"  FIT {label} {act['start_local'][:10]} ({act['id']}) ...")
        try:
            raw = client.download_activity(int(act['id']), dl_fmt=client.ActivityDownloadFormat.ORIGINAL)
            fit_bytes = zipfile.ZipFile(io.BytesIO(raw)).read(zipfile.ZipFile(io.BytesIO(raw)).namelist()[0])
        except Exception as e:
            print(f"    skip: {e}")
            continue
        laps = parse_fit_biomechanics(fit_bytes)
        if not laps:
            # Nessun lap sforzo — salva comunque un segnaposto per non riscaricare
            rep_bio.append({'id': act['id'], 'date': act['start_local'][:10],
                            'label': label, 'reps': [], 'avg': {}})
            continue

        def _avg(key):
            vals = [l[key] for l in laps if l.get(key) is not None]
            return round(sum(vals)/len(vals), 1) if vals else None

        entry = {
            'id': act['id'],
            'date': act['start_local'][:10],
            'label': label,
            'reps': laps,
            'avg': {
                'gct_ms': _avg('gct_ms'),
                'cadence_spm': int(_avg('cadence_spm')) if _avg('cadence_spm') else None,
                'osc_mm': _avg('osc_mm'),
                'step_mm': int(_avg('step_mm')) if _avg('step_mm') else None,
            }
        }
        rep_bio.append(entry)
        print(f"    {len(laps)} lap · GCT={entry['avg']['gct_ms']}ms "
              f"cad={entry['avg']['cadence_spm']}spm osc={entry['avg']['osc_mm']}mm")

    rep_bio.sort(key=lambda x: x['date'])
    biomech['rep_biomechanics'] = rep_bio
    if not dry_run:
        biomech_path.write_text(json.dumps(biomech, indent=2, ensure_ascii=False), encoding='utf-8')
        print(f"  [json] {biomech_path}")


def sync_garmin_activities(client, out_path: Path, start_date: date, dry_run: bool):
    """Backfill e aggiornamento continuo di garmin_activities.json.
    Usa paginazione offset-based per recuperare tutte le attività senza limiti di finestra."""
    existing = {}
    if out_path.exists():
        for act in json.loads(out_path.read_text(encoding="utf-8")):
            existing[act["id"]] = act

    start_iso = start_date.isoformat()
    new_count = 0
    offset = 0
    page_size = 100

    while True:
        print(f"  Garmin activities offset={offset} limit={page_size} ...")
        raw = safe(client.get_activities, offset, page_size)
        if not raw:
            break
        added_this_page = 0
        for a in raw:
            # Salta attività antecedenti alla data di inizio
            start_local = (a.get("startTimeLocal") or "").replace(" ", "T")
            if start_local and start_local[:10] < start_iso:
                continue
            norm = normalize_activity(a)
            if norm and norm["id"] and norm["id"] not in existing:
                existing[norm["id"]] = norm
                new_count += 1
                added_this_page += 1
        if len(raw) < page_size:
            break  # ultima pagina
        offset += page_size

    acts = sorted(existing.values(), key=lambda x: x["start_local"])
    print(f"  Totale: {len(acts)} attivita ({new_count} nuove)")
    if not dry_run:
        out_path.write_text(json.dumps(acts, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  [json] {out_path}")


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
        write_latest(all_wellness, all_activities, out_dir)


def write_latest(all_wellness, all_activities, out_dir: Path):
    today_w = all_wellness[0] if all_wellness else {}
    lines = [
        f"# Garmin latest — {today_w.get('date', 'n/d')}",
        "",
        "## Wellness oggi",
    ]
    lines.append(wellness_to_md(today_w).replace(f"# Garmin wellness {today_w.get('date','')}\n", "").strip())

    recent_acts = [(d, a) for d, acts in all_activities for a in acts]
    if recent_acts:
        lines += ["", "## Attività recenti"]
        for day, a in recent_acts[:5]:
            _, md = activity_to_md(a, day)
            lines.append(md.strip())
            lines.append("")

    path = out_dir / "latest.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  [latest] {path}")


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
    parser.add_argument("--activities", action="store_true",
                        help="Sincronizza anche garmin_activities.json")
    parser.add_argument("--backfill", default="2026-02-01",
                        help="Data inizio backfill attività (default 2026-02-01)")
    parser.add_argument("--biomech", action="store_true",
                        help="Scarica FIT e aggiorna biomech.json con dati biomeccanici")
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

    if args.activities:
        print("\nSync attivita Garmin ...")
        start = date.fromisoformat(args.backfill)
        sync_garmin_activities(client, Path("garmin_activities.json"), start, args.dry_run)

    if args.biomech:
        print("\nSync biomeccanica FIT ...")
        sync_biomechanics(client, Path("garmin_activities.json"), Path("biomech.json"), args.dry_run)

    print("Fatto.")


if __name__ == "__main__":
    main()
