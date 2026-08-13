<img src="assets/logo.png" width="100" height="100" align="right" />

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

# StravaMerger

StravaMerger is a Python automation for cleaning up recent Strava activities. It can:

- merge contiguous activities of the same sport into one replacement activity;
- find GPS recording holes inside individual activities;
- obtain missing route geometry from the Google Maps Routes API;
- interpolate timestamps and elevation across the missing section;
- back up source and replacement GPX files, upload replacements, and optionally email links;
- resume duplicate-rejected uploads after the original activity is manually deleted.

The hole-repair feature is a headless integration of the useful parts of the older
[`gpxfix`](https://github.com/jannisborn/gpxfix) project. Its Tk GUI and manual GPX
fragment workflow are not used.

## How it works

Each run retrieves the requested number of recent activities. Merge candidates are
selected when their sport matches and their endpoints are sufficiently close. By
default, they must start on the same local day; the existing overnight-continuation
behavior is retained.

When `--fix-holes` is supplied, every eligible, unprocessed GPS track is also
inspected for adjacent points where:

- the timestamp jump is greater than `--hole-time-threshold` (default: 5 seconds), and
- the straight-line jump is greater than `--hole-distance-threshold` (default: 400 m).

For each hole, StravaMerger asks the Google Routes API for a high-quality route between
the surviving endpoints. It inserts the returned geometry into the original time
window and linearly interpolates elevation. Existing points and their heart-rate and
temperature extensions are retained.

The automatic travel modes are intentionally conservative:

| Strava sport | Google mode |
| --- | --- |
| Ride, MountainBikeRide, GravelRide, EBikeRide and related cycle sports | `BICYCLE` |
| Run, TrailRun, Walk, Hike, Wheelchair | `WALK` |
| Other sports | manual review; no route is invented |

Travel mode is always derived from the activity's Strava sport and cannot be
overridden. Internal safety limits reject a direct gap over 20 km, a routed distance
over four times the direct distance, or an activity with more than five holes. A
rejected or unsupported repair is left unchanged, recorded in the state file, and
reported by email when enabled.

## Strava replacement lifecycle

Strava does not expose an API operation for deleting activities. A replacement may
also be rejected while its source activity exists because Strava detects it as a
duplicate. StravaMerger therefore uses this workflow:

1. Save source GPX backups and the replacement GPX in `--ofolder`.
2. Record a durable job in `<ofolder>/.stravamerger-state.json`.
3. Email links to the source activities that must be deleted, including detected GPS
   hole distances, when email is enabled.
4. Attempt the replacement upload.
5. If Strava reports a duplicate, wait for source deletion and retry the saved file on
   a later run.
6. Email the new Strava link after a successful upload, when email is enabled.

Do not delete the state file while jobs are pending. It prevents repeated repairs,
merges, emails, and Google routing calls. Activities already checked and found clean
are also recorded there. Delete the state file only when you intentionally want a full
rescan.

Replacing an activity does not preserve its kudos, comments, photos, existing segment
results, or original device attribution. The original GPX backup remains in the output
folder.

## Installation

```console
uv sync
```

This creates a local `.venv` and installs the project dependencies.

## API setup

### Strava

Create a [Strava API application](https://www.strava.com/settings/api) and authorize it
with `activity:read`, `activity:read_all`, and `activity:write`. Create a writable
`secret.json` file:

```json
{
  "client_id": 123456,
  "client_secret": "STRAVA_CLIENT_SECRET",
  "access_token": "STRAVA_ACCESS_TOKEN",
  "refresh_token": "STRAVA_REFRESH_TOKEN",
  "mail": "GMAIL_APP_PASSWORD",
  "google_maps_api_key": "YOUR_GOOGLE_MAPS_API_KEY"
}
```

Strava can rotate the refresh token. StravaMerger writes the latest access token,
refresh token, and expiration back to this file atomically, so the file must remain
writable. Keep it out of version control; `*secret.json` is ignored by this repository.

### Google Maps

Enable billing and the
[Google Maps Routes API](https://developers.google.com/maps/documentation/routes) for
the API key's Google Cloud project. Restrict the key to the Routes API and, where your
deployment permits it, to the server's source IP.

Store the key as `google_maps_api_key` in `secret.json`, as shown above.

Google Maps Platform usage is billable and governed by Google's current terms. In
particular, review the applicable restrictions before storing routed geometry or using
it outside a Google map. Hole filling is disabled by default; use `--fix-holes` to opt
in after configuring Google. The merge automation continues to work without it.
Hole endpoint coordinates are sent to Google whenever a route is requested.

### Email

The current notifier uses Gmail SMTP. `mail` must therefore be an app password for the
address passed to `--sender`, not the account's normal password. Email is disabled by
default; add `--sender your-address@gmail.com` to enable deletion, confirmation, and
review messages. Without email, source IDs remain available in the state file and logs.

## Usage

Run both merge and hole-repair automation:

```console
uv run stravamerger \
  --credentials secret.json \
  --n_activities 21 \
  --distance 500 \
  --ofolder data/ \
  --recipient name@example.com \
  --sender your-address@gmail.com \
  --fix-holes
```

Only merge activities:

```console
uv run stravamerger \
  --credentials secret.json \
  --n_activities 21 \
  --distance 500 \
  --ofolder data/ \
  --recipient name@example.com \
  --sender your-address@gmail.com
```

Require the same bike or shoes across merge candidates:

```console
uv run stravamerger \
  --credentials secret.json \
  --n_activities 21 \
  --distance 500 \
  --ofolder data/ \
  --recipient name@example.com \
  --sender your-address@gmail.com \
  --require-same-gear
```

Useful repair options:

```text
--fix-holes / --no-fix-holes  (default: no-fix-holes)
--hole-time-threshold FLOAT
--hole-distance-threshold FLOAT
--state PATH
```

Run `uv run stravamerger --help` for the complete current CLI reference.

## Scheduling

The command remains a one-shot process, making it suitable for cron, a systemd timer,
launchd, or another scheduler. For example, this checks the latest activities every
hour:

```cron
15 * * * * cd /absolute/path/to/stravamerger && /absolute/path/to/uv run stravamerger --credentials /absolute/path/to/secret.json --n_activities 21 --distance 500 --ofolder /absolute/path/to/data --recipient name@example.com --sender your-address@gmail.com
```

Use absolute paths in scheduled jobs. Keep the same `--ofolder` or explicit `--state`
path between runs so pending uploads can resume.

## Per-activity controls

Add these case-insensitive markers to a Strava activity description:

- `nomerge`: exclude the activity from merge matching;
- `nofix`: exclude the activity from GPS-hole repair.

Activities created by StravaMerger are automatically excluded from both operations.

## Development checks

```console
uv run python -m unittest discover -s tests -v
```
