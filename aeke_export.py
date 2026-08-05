#!/usr/bin/env python3
"""
aeke-export — pull your own AEKE smart-gym data from the AEKE cloud API.

The AEKE Fit app has no official export or Apple Health / Google Fit sync. This
tool talks to the same private REST API the app uses, so you can get your body-
composition history out as JSON or InfluxDB line protocol.

It only ever reads. It authenticates with a bearer token that you supply — see
the README for how to capture one from the app's traffic. Nothing here is
specific to one account; point it at your own token and it pulls your own data.

Zero third-party dependencies: standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = os.environ.get("AEKE_BASE", "service-us.aeke.com")
DEFAULT_UNITS = os.environ.get("AEKE_UNITS", "metric")  # "metric" (kg) or "imperial" (lb)
CLIENT_VERSION = os.environ.get("AEKE_CLIENT_VERSION", "2.8.1")
USER_AGENT = os.environ.get(
    "AEKE_USER_AGENT", f"aeke/{CLIENT_VERSION} (aeke-export; +github)"
)


class AekeError(RuntimeError):
    pass


class AekeClient:
    """Thin client over the AEKE cloud REST API. Read-only."""

    def __init__(self, token: str, base: str = DEFAULT_BASE, units: str = DEFAULT_UNITS,
                 timezone: str | None = None, timeout: float = 20.0):
        if not token:
            raise AekeError("no token (set AEKE_TOKEN or pass --token)")
        self.token = token.strip()
        self.base = base
        self.units = units
        self.timezone = timezone or os.environ.get("AEKE_TZ", "UTC")
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": self.token,
            "user-agent": USER_AGENT,
            "client-version": CLIENT_VERSION,
            "client": "ios",
            "client-language": "en_US",
            "client-timezone": self.timezone,
            "client-weight-system": self.units,
            "client-height-system": self.units,
            "accept": "*/*",
            "content-type": "application/json;charset=utf-8",
        }

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"https://{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raise AekeError(f"{method} {path} -> HTTP {e.code}: {e.read()[:200]!r}") from e
        except urllib.error.URLError as e:
            raise AekeError(f"{method} {path} -> {e.reason}") from e
        if payload.get("code") != "SUCCESS":
            raise AekeError(f"{method} {path} -> API code={payload.get('code')} msg={payload.get('msg')!r}")
        return payload.get("data", {})

    # --- endpoints ------------------------------------------------------
    def profile(self) -> dict:
        return self._call("GET", "/api/user/profile")

    def measurements(self) -> list[dict]:
        """List of past body-scale measurements: [{no, date}, ...]."""
        return self._call("GET", "/api/user-scale/v1/list").get("recordList", [])

    def fitness_data(self) -> dict:
        """Full body-composition detail for the LATEST measurement.

        Note: the API ignores the measurement id and always returns the most
        recent weigh-in, so this builds a forward time series (one point per
        new measurement), not a historical backfill.
        """
        return self._call("POST", "/api/user-scale/v1/fitnessData", {"no": ""})

    def course_histories(self, month: str) -> list[dict]:
        """Course-based workout history for a 'YYYY-MM' month.

        Empty for free-mode training — the cloud only keeps aggregate minutes
        (see profile.sportMinute) for non-course sessions.
        """
        return self._call("POST", "/api/user/histories", {"date": month}).get("histories", [])


# --- flattening -------------------------------------------------------------
# fitnessData carries three shapes of number:
#   1. nested trend objects: {"value": N, "trend": ..., "changeValue": ...}
#   2. plain top-level scalars: "weight": 66.8
#   3. per-segment objects: {"muscleOrFatMass": N, "muscleOrFatRate": ..., "rateCode": ...}
# We reduce all of them to one flat {metric_name: number} dict for storage.

# Nested {value: N} metrics -> readable names. Keys not listed pass through raw.
_NESTED_NAMES = {
    "bmi": "bmi",
    "bmr": "bmr_kcal",
    "bodyAge": "body_age",
    "bodyFat": "body_fat_pct",
    "subcutaneousFat": "subcutaneous_fat_pct",
    "vfr": "visceral_fat_rating",
    "whr": "waist_hip_ratio",
    "bwp": "body_water_pct",
    "ssm": "skeletal_muscle_pct",
    "pp": "protein_pct",
    "muscleMass": "muscle_mass_kg",
    "fatMass": "fat_mass_kg",
    "bmd": "bone_mass_kg",
}

# Plain top-level scalars worth keeping (skips ids, codes, enums like gender/btCode).
_SCALAR_NAMES = {
    "weight": "weight_kg",
    "height": "height_cm",
    "originWeight": "origin_weight_kg",
    "targetWeight": "target_weight_kg",
    "sbw": "standard_body_weight_kg",
    "score": "score",
    "weightChangeValue": "weight_change_kg",
    "usersAbovePercentage": "users_above_pct",
}

# Segment analysis: base key holds FAT for that part, base+"Muscle" holds MUSCLE.
_SEGMENTS = {
    "allBody": "trunk",       # AEKE labels the trunk/whole segment "allBody"
    "leftArm": "left_arm",
    "rightArm": "right_arm",
    "leftLeg": "left_leg",
    "rightLeg": "right_leg",
}


def flatten_fitness(data: dict) -> dict:
    """Reduce a fitnessData payload to a flat {metric: number} dict.

    Includes whole-body composition and per-limb (segment) muscle/fat mass.
    """
    out: dict[str, float] = {}

    # 1. nested {value} metrics
    for k, v in data.items():
        if isinstance(v, dict) and isinstance(v.get("value"), (int, float)):
            out[_NESTED_NAMES.get(k, k)] = v["value"]

    # 2. plain scalars we care about
    for src, dst in _SCALAR_NAMES.items():
        v = data.get(src)
        if isinstance(v, (int, float)):
            out[dst] = v

    # 3. segment analysis — muscle and fat mass per body part
    for src, name in _SEGMENTS.items():
        fat = data.get(src)
        if isinstance(fat, dict) and isinstance(fat.get("muscleOrFatMass"), (int, float)):
            out[f"{name}_fat_kg"] = fat["muscleOrFatMass"]
        muscle = data.get(src + "Muscle")
        if isinstance(muscle, dict) and isinstance(muscle.get("muscleOrFatMass"), (int, float)):
            out[f"{name}_muscle_kg"] = muscle["muscleOrFatMass"]

    return out


def measurement_time_ns(data: dict) -> int | None:
    """Nanosecond timestamp of the weigh-in itself (idempotent re-polling)."""
    ms = data.get("createdAtTimestamp")
    if isinstance(ms, (int, float)):
        return int(ms * 1_000_000)
    return None


def token_days_remaining(token: str) -> float | None:
    """Days until the bearer JWT expires, or None if it can't be read.

    This API puts `exp` in milliseconds (not the JWT-standard seconds), so
    values above ~1e12 are treated as ms.
    """
    try:
        import base64
        part = token.strip().split()[-1].split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        exp = payload.get("exp")
        if exp is None:
            return None
        if exp > 1e12:
            exp /= 1000.0
        return (exp - time.time()) / 86400.0
    except Exception:
        return None


# --- output formats ---------------------------------------------------------
def _lp_escape(v: str) -> str:
    return v.replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def to_line_protocol(metrics: dict, measurement: str = "aeke_body",
                     tags: dict | None = None, ts_ns: int | None = None) -> str:
    """Render a flat metric dict as one InfluxDB line-protocol line."""
    tagset = "".join(f",{_lp_escape(k)}={_lp_escape(str(v))}" for k, v in (tags or {}).items())
    fields = ",".join(f"{k}={float(v)}" for k, v in metrics.items())
    if not fields:
        raise AekeError("no numeric fields to write")
    ts = f" {ts_ns}" if ts_ns is not None else ""
    return f"{measurement}{tagset} {fields}{ts}"


def write_influx(line: str) -> None:
    """Write line protocol to InfluxDB.

    Picks v1 or v2 by which env vars are set:
      v1: INFLUX_V1_URL + INFLUX_V1_DB (+ optional INFLUX_V1_USER / _PASSWORD)
      v2: INFLUX_URL + INFLUX_TOKEN + INFLUX_ORG + INFLUX_BUCKET
    """
    v1_url = os.environ.get("INFLUX_V1_URL")
    if v1_url:
        db = os.environ.get("INFLUX_V1_DB")
        if not db:
            raise AekeError("set INFLUX_V1_DB for InfluxDB v1")
        import urllib.parse
        q = {"db": db, "precision": "ns"}
        user = os.environ.get("INFLUX_V1_USER")
        pw = os.environ.get("INFLUX_V1_PASSWORD")
        if user:
            q["u"] = user
            q["p"] = pw or ""
        endpoint = f"{v1_url.rstrip('/')}/write?{urllib.parse.urlencode(q)}"
        headers = {"Content-Type": "text/plain; charset=utf-8"}
    else:
        url = os.environ.get("INFLUX_URL")
        token = os.environ.get("INFLUX_TOKEN")
        org = os.environ.get("INFLUX_ORG")
        bucket = os.environ.get("INFLUX_BUCKET")
        if not all([url, token, org, bucket]):
            raise AekeError("set INFLUX_V1_URL+INFLUX_V1_DB (v1), or "
                            "INFLUX_URL+INFLUX_TOKEN+INFLUX_ORG+INFLUX_BUCKET (v2)")
        endpoint = f"{url.rstrip('/')}/api/v2/write?org={org}&bucket={bucket}&precision=ns"
        headers = {"Authorization": f"Token {token}", "Content-Type": "text/plain; charset=utf-8"}

    req = urllib.request.Request(endpoint, data=line.encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status not in (200, 204):
                raise AekeError(f"influx write -> HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        raise AekeError(f"influx write -> HTTP {e.code}: {e.read()[:200]!r}") from e


# --- CLI --------------------------------------------------------------------
def _client(args) -> AekeClient:
    token = args.token or os.environ.get("AEKE_TOKEN", "")
    return AekeClient(token, base=args.base, units=args.units, timezone=args.tz)


def cmd_profile(args):
    print(json.dumps(_client(args).profile(), indent=2, ensure_ascii=False))


def cmd_measurements(args):
    print(json.dumps(_client(args).measurements(), indent=2, ensure_ascii=False))


def cmd_latest(args):
    data = _client(args).fitness_data()
    if args.raw:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(flatten_fitness(data), indent=2, ensure_ascii=False))


def cmd_influx(args):
    c = _client(args)
    data = c.fitness_data()
    metrics = flatten_fitness(data)
    # Timestamp the point at the weigh-in's own moment so re-polling the same
    # measurement overwrites the same point instead of piling up duplicates.
    ts_ns = measurement_time_ns(data) or int(time.time() * 1e9)
    tags = {"units": args.units, "no": data.get("no", "unknown")}
    line = to_line_protocol(metrics, tags=tags, ts_ns=ts_ns)
    if args.dry_run:
        print(line)
        return
    write_influx(line)
    sys.stderr.write(f"wrote {len(metrics)} fields to influx\n")


def cmd_tokeninfo(args):
    token = args.token or os.environ.get("AEKE_TOKEN", "")
    if not token:
        raise AekeError("no token (set AEKE_TOKEN or pass --token)")
    days = token_days_remaining(token)
    if days is None:
        print("token: unreadable / not a JWT")
        return 1
    print(f"token expires in {days:.1f} days")
    if days <= 0:
        return 1
    if args.warn_days is not None and days <= args.warn_days:
        return 2
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="aeke-export", description=__doc__.strip().splitlines()[0])
    p.add_argument("--token", help="bearer token (default: $AEKE_TOKEN)")
    p.add_argument("--base", default=DEFAULT_BASE, help=f"API host (default: {DEFAULT_BASE})")
    p.add_argument("--units", default=DEFAULT_UNITS, choices=["metric", "imperial"],
                   help=f"unit system (default: {DEFAULT_UNITS})")
    p.add_argument("--tz", default=os.environ.get("AEKE_TZ", "UTC"), help="client timezone")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("profile", help="print your user profile").set_defaults(func=cmd_profile)
    sub.add_parser("measurements", help="list past body-scale measurement dates").set_defaults(func=cmd_measurements)

    sp = sub.add_parser("latest", help="latest body-composition metrics")
    sp.add_argument("--raw", action="store_true", help="print the full nested payload")
    sp.set_defaults(func=cmd_latest)

    sp = sub.add_parser("influx", help="write latest metrics to InfluxDB (or --dry-run)")
    sp.add_argument("--dry-run", action="store_true", help="print line protocol instead of writing")
    sp.set_defaults(func=cmd_influx)

    sp = sub.add_parser("token-info", help="report days until the token expires")
    sp.add_argument("--warn-days", type=float, default=None,
                    help="exit 2 if the token expires within this many days")
    sp.set_defaults(func=cmd_tokeninfo)

    args = p.parse_args(argv)
    try:
        rc = args.func(args)
    except AekeError as e:
        sys.stderr.write(f"error: {e}\n")
        return 1
    return rc or 0


if __name__ == "__main__":
    raise SystemExit(main())
