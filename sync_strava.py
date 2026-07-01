#!/usr/bin/env python3
"""
Strava -> strava_activities.json sync.
Usa OAuth2 con refresh token (nessuna interazione umana dopo il primo setup).

Setup una tantum:
  1. Vai su https://www.strava.com/settings/api e crea un'app (qualsiasi nome/URL)
  2. Nota CLIENT_ID e CLIENT_SECRET
  3. Apri nel browser (sostituisci CLIENT_ID):
     https://www.strava.com/oauth/authorize?client_id=CLIENT_ID&response_type=code&redirect_uri=http://localhost&scope=activity:read_all
  4. Dopo l'ok Strava ti reindirizza su localhost?code=XXXX — copia quel codice
  5. Esegui: python sync_strava.py --exchange-code XXXX
     Stampa il refresh token da mettere in STRAVA_REFRESH_TOKEN su GitHub

Uso normale (in GitHub Actions):
  python sync_strava.py --days 3
"""

import argparse
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# bypass proxy SSL inspection (comune in reti ospedaliere)
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

CLIENT_ID     = os.environ.get("STRAVA_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "")
REFRESH_TOKEN = os.environ.get("STRAVA_REFRESH_TOKEN", "")
OUT_FILE      = Path("strava_activities.json")
LAPS_FILE     = Path("strava_laps.json")

BASE = "https://www.strava.com/api/v3"


# ── Auth ───────────────────────────────────────────────────────────────────────

def exchange_code(code: str):
    """Scambia il codice auth con un refresh token (una tantum)."""
    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "code": code, "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request("https://www.strava.com/oauth/token", data=data)
    with urllib.request.urlopen(req, context=_ssl_ctx) as r:
        resp = json.loads(r.read())
    print(f"\nAtleta: {resp['athlete']['firstname']} {resp['athlete']['lastname']}")
    print(f"\n=== STRAVA_REFRESH_TOKEN ===\n{resp['refresh_token']}\n=== FINE ===\n")
    print("Aggiungi questo valore come secret STRAVA_REFRESH_TOKEN su GitHub.")


def get_access_token() -> str:
    if not CLIENT_ID or not CLIENT_SECRET or not REFRESH_TOKEN:
        sys.exit("Imposta STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN.")
    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN, "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://www.strava.com/oauth/token", data=data)
    with urllib.request.urlopen(req, context=_ssl_ctx) as r:
        resp = json.loads(r.read())
    return resp["access_token"]


# ── Fetch ──────────────────────────────────────────────────────────────────────

def fetch_activities(token: str, after_ts: int) -> list:
    acts = []
    page = 1
    while True:
        url = f"{BASE}/athlete/activities?after={after_ts}&per_page=100&page={page}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, context=_ssl_ctx) as r:
            batch = json.loads(r.read())
        if not batch:
            break
        acts.extend(batch)
        page += 1
    return acts


def normalize(a: dict) -> dict:
    return {
        "id": str(a["id"]),
        "name": a.get("name", ""),
        "sport_type": a.get("sport_type", a.get("type", "")),
        "start_local": a.get("start_date_local", "")[:19],
        "distance": round(a.get("distance", 0), 1),
        "moving_time": a.get("moving_time", 0),
        "elevation_gain": round(a.get("total_elevation_gain", 0), 1),
        "avg_speed": round(a.get("average_speed", 0), 4),
        "avg_cadence": round(a["average_cadence"] * 2, 1) if a.get("average_cadence") else None,
        "avg_hr": a.get("average_heartrate"),
        "max_hr": a.get("max_heartrate"),
        "relative_effort": a.get("suffer_score"),
        "calories": a.get("calories"),
        "kudos_count": a.get("kudos_count", 0),
    }


# ── Laps ───────────────────────────────────────────────────────────────────────

def fetch_laps(token: str, act_id: str) -> list:
    url = f"{BASE}/activities/{act_id}/laps"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  warn: laps {act_id} fallito: {e}")
        return []


def normalize_lap(lap: dict) -> dict:
    spd = lap.get("average_speed", 0)
    cad = lap.get("average_cadence")
    return {
        "pace_s": round(1000 / spd) if spd else None,
        "cadence_spm": round(cad * 2) if cad else None,
        "hr": lap.get("average_heartrate"),
        "dist_m": round(lap.get("distance", 0)),
        "elapsed_s": lap.get("elapsed_time", 0),
    }


# ── Merge ──────────────────────────────────────────────────────────────────────

def merge(existing: list, new_acts: list) -> list:
    by_id = {a["id"]: a for a in existing}
    for a in new_acts:
        by_id[a["id"]] = a
    return sorted(by_id.values(), key=lambda a: a["start_local"])


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exchange-code", metavar="CODE", help="Scambia codice OAuth con refresh token")
    parser.add_argument("--days", type=int, default=3, help="Giorni di storia da scaricare")
    parser.add_argument("--all", action="store_true", help="Scarica tutta la storia disponibile")
    parser.add_argument("--fetch-laps", action="store_true", help="Scarica lap splits per le attività (aggiorna strava_laps.json)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.exchange_code:
        exchange_code(args.exchange_code)
        return

    token = get_access_token()

    if args.all:
        after_ts = 0
    else:
        after_dt = datetime.now(timezone.utc) - timedelta(days=args.days)
        after_ts = int(after_dt.timestamp())

    print(f"Fetching attività Strava (after={after_ts})...")
    raw = fetch_activities(token, after_ts)
    print(f"  trovate {len(raw)} attività")

    new_acts = [normalize(a) for a in raw]

    existing = json.loads(OUT_FILE.read_text(encoding="utf-8")) if OUT_FILE.exists() else []
    merged = merge(existing, new_acts)

    if args.dry_run:
        for a in merged[-5:]:
            print(f"  {a['start_local'][:10]} {a['name']} {a['distance']/1000:.1f}km")
    else:
        OUT_FILE.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  scritto {OUT_FILE} ({len(merged)} attività totali)")

    # ── Lap splits ─────────────────────────────────────────────────────────────
    if args.fetch_laps and not args.dry_run:
        existing_laps = json.loads(LAPS_FILE.read_text(encoding="utf-8")) if LAPS_FILE.exists() else {}
        # Fetch laps per tutte le Run non ancora presenti
        runs_to_fetch = [a for a in merged if a.get("sport_type") == "Run" and a["id"] not in existing_laps]
        print(f"Fetching laps per {len(runs_to_fetch)} run (già presenti: {len(existing_laps)})...")
        for i, a in enumerate(runs_to_fetch):
            raw_laps = fetch_laps(token, a["id"])
            existing_laps[a["id"]] = [normalize_lap(l) for l in raw_laps]
            if (i + 1) % 10 == 0:
                print(f"  {i+1}/{len(runs_to_fetch)}...")
        LAPS_FILE.write_text(json.dumps(existing_laps, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  scritto {LAPS_FILE} ({len(existing_laps)} attività con laps)")

    print("Fatto.")


if __name__ == "__main__":
    main()
