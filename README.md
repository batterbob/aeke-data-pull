# aeke-export

Pull your own [AEKE](https://aeke.com) smart-gym data out of the AEKE cloud.

The AEKE Fit app has no official data export and no Apple Health / Google Fit
sync. This is a small, read-only, zero-dependency Python tool that talks to the
same private REST API the app uses, so you can get your **body-composition
history** out as JSON or push it to InfluxDB for graphing.

> Unofficial and unaffiliated. It uses an undocumented API that AEKE can change
> or break at any time. It only reads your own account, using a token you
> supply. Use it on your own data.

## What you can get

| Command | Data |
| --- | --- |
| `profile` | Your profile: height, weight, targets, lifetime training minutes |
| `measurements` | Dates of your past body-scale weigh-ins |
| `latest` | Full body composition for your most recent weigh-in — 30+ metrics: weight, body-fat %, BMI, muscle/fat/bone/water mass, skeletal-muscle %, visceral fat, BMR, protein %, waist-hip ratio, body age, **plus per-limb segment analysis** (trunk, both arms, both legs — muscle and fat kg each) |
| `influx` | The above as InfluxDB line protocol (stdout or direct write) |

### What you *cannot* get (yet)

Per-workout detail — reps, sets, resistance, which exercise — does **not** appear
in the cloud API for free-mode training; the cloud keeps only an aggregate
"total training minutes" number (`profile.sportMinute`). Course-based workouts
land in `/api/user/histories`, which is empty if you don't follow guided
courses. Live per-rep data likely stays on the device / its MQTT channel.

## Requirements

Python 3.9+. No third-party packages.

## Getting a token

The API authenticates with a bearer JWT (valid ~30 days). Capture yours once
from the app's own traffic:

1. Run an intercepting proxy on your LAN — [mitmproxy](https://mitmproxy.org) is
   easiest: `mitmweb --web-host 0.0.0.0`.
2. On your phone, set that proxy under Wi-Fi settings and install + **trust**
   mitmproxy's CA (on iOS: install the profile, then General → About →
   Certificate Trust Settings → toggle it on).
3. Open the AEKE Fit app and view your body-scale report.
4. In the proxy, find a request to `service-us.aeke.com` and copy its
   `authorization` header value. That is your token.

(If your account region differs, the host may be e.g. `service-cn.aeke.com`;
pass it with `--base`.)

## Usage

```bash
export AEKE_TOKEN='eyJhbGci...'     # the authorization header value
export AEKE_TZ='America/New_York'   # optional

python3 aeke_export.py latest                 # flattened metrics as JSON
python3 aeke_export.py latest --raw           # full nested payload
python3 aeke_export.py measurements
python3 aeke_export.py profile
python3 aeke_export.py influx --dry-run        # preview line protocol
```

### Writing to InfluxDB

Supports both InfluxDB 1.x and 2.x — set one block of env vars and drop
`--dry-run`.

InfluxDB 1.x:

```bash
export INFLUX_V1_URL='http://localhost:8086'
export INFLUX_V1_DB='fitness'
export INFLUX_V1_USER='writer'        # if auth is enabled
export INFLUX_V1_PASSWORD='...'
python3 aeke_export.py influx
```

InfluxDB 2.x:

```bash
export INFLUX_URL='http://localhost:8086'
export INFLUX_TOKEN='...'
export INFLUX_ORG='home'
export INFLUX_BUCKET='aeke'
python3 aeke_export.py influx
```

Run it on a schedule (e.g. cron after your usual weigh-in time) to build a time
series. `fitnessData` always returns your latest measurement, so each run adds
one point — it does not backfill history.

Data lands as measurement `aeke_body`, tagged `units` and `no` (the weigh-in id).

## Options

| Flag / env | Meaning | Default |
| --- | --- | --- |
| `--token` / `AEKE_TOKEN` | bearer token | — (required) |
| `--base` / `AEKE_BASE` | API host | `service-us.aeke.com` |
| `--units` / `AEKE_UNITS` | `metric` (kg) or `imperial` (lb) | `metric` |
| `--tz` / `AEKE_TZ` | client timezone | `UTC` |

## License

MIT — see [LICENSE](LICENSE).
