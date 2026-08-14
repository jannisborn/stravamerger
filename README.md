<img src="assets/logo.png" width="100" height="100" align="right" />

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

# StravaMerger

StravaMerger automates cleanup of recent Strava activities. It can merge contiguous
activities, repair GPS recording holes through Google Maps, save GPX backups, upload
replacements, and email the actions that still require manual source deletion. The
hole repair is a headless integration of the useful parts of
[`gpxfix`](https://github.com/jannisborn/gpxfix).

## Behavior

Merge candidates must have the same sport, nearby endpoints, and normally the same
local date. `--require-same-gear` additionally requires the same non-empty `gear_id`.

With `--fix-holes`, an unchecked track has a hole when adjacent points exceed both:

- `--hole-time-threshold` (default: 5 seconds), and
- `--hole-distance-threshold` (default: 400 m straight-line).

Google Routes supplies the missing geometry. StravaMerger interpolates timestamps and
elevation while retaining existing points and their heart-rate and temperature data.
Hole endpoints are reverse geocoded for logs and email; coordinates are used if
geocoding fails.

Travel mode comes from the Strava sport: cycle sports use `BICYCLE`; runs, walks, and
hikes use `WALK`; unsupported sports require manual review. More than five holes or a
direct gap over 20 km is rejected. If Google returns no usable route, or its route is
over four times the direct gap, the replacement uses straight-line GPX coordinates at
intervals of at most three seconds. The email says so; add `nomerge` instead of
deleting the source if that repair is not acceptable.

Repaired tracks keep their name unless it starts with `Fahrt am ...` or `Lauf am ...`.
For those generic names, a matching location in `NAME_DICT` supplies a route name such
as `IBM`.

## Fetching and persistent state

`--n_activities` controls the recent activity window, not the number fully downloaded:

1. StravaMerger lists summary data for the window.
2. It fetches full details for pending jobs and summary-based merge candidates.
3. For hole detection, it fetches the description of each previously unchecked
   activity and, unless `nomerge` or `nofix` is present, fetches its GPS stream.
4. Only the GPS stream can reveal whether a hole exists; summary statistics cannot.

The program monitors
[Strava's read-limit headers](https://developers.strava.com/docs/rate-limits/), keeps
ten requests in reserve, and stops starting new checks when that reserve is reached.
Completed checks are recorded in `<ofolder>/.stravamerger-state.json`. On the next
scheduled run they are skipped, so an initial window such as 200 activities is scanned
across multiple runs rather than all at once.

Queued replacements embed their GPX data in the state file and are uploaded from
memory. The separate GPX files in `--ofolder` are backups, not queue dependencies, so
removing them does not break later retries. Older file-only queue entries are rebuilt
from their Strava sources when those sources still exist.

There is currently no `--force-refresh` flag. To perform a complete rescan, first
resolve all pending replacements, then delete the state file. Do not delete it while
jobs are pending: it also contains saved upload and notification state.

## Replacement workflow and email

Strava does not allow API deletion of activities. StravaMerger therefore:

1. saves source and replacement GPX files in `--ofolder`;
2. attempts the replacement upload and records the job;
3. emails the current source-deletion checklist on every run while action remains;
4. retries duplicate-rejected uploads after the source disappears and sends one
   confirmation email after a successful upload.

Deleted sources disappear from the next reminder. Add these case-insensitive markers
to a Strava activity description:

- `nomerge`: skip merging and hole repair, and cancel a pending replacement;
- `nofix`: skip hole repair.

Activities created by StravaMerger are excluded automatically.

## Setup

```console
uv sync
```

Create a writable `secret.json`:

```json
{
  "client_id": 123456,
  "client_secret": "STRAVA_CLIENT_SECRET",
  "access_token": "STRAVA_ACCESS_TOKEN",
  "refresh_token": "STRAVA_REFRESH_TOKEN",
  "mail": "GMAIL_APP_PASSWORD",
  "google_maps_api_key": "GOOGLE_MAPS_API_KEY"
}
```

The Strava application needs `activity:read`, `activity:read_all`, and
`activity:write`. StravaMerger persists rotated tokens back to this file.

For hole repair, enable billing plus the Google Maps Routes and Geocoding APIs and
restrict the key to those APIs and, when possible, the server IP. Google usage is
billable: each hole needs one route request and up to two reverse-geocoding requests.

Email uses Gmail SMTP. `mail` must be an app password for the address passed to
`--sender`. Omit `--sender` to disable email.

## Usage

```console
uv run stravamerger \
  --credentials secret.json \
  --n_activities 200 \
  --distance 800 \
  --ofolder tracks/ \
  --recipient name@example.com \
  --sender your-address@gmail.com \
  --require-same-gear \
  --fix-holes
```

Hole repair defaults to off; omit `--fix-holes` for merge-only operation. Use the same
`--ofolder` or explicit `--state` path on every cron run so queued jobs and scan
progress resume. Use absolute paths in cron. Run `uv run stravamerger --help` for all
options.

Replacing an activity does not preserve kudos, comments, photos, existing segment
results, or original device attribution. Source GPX backups remain in the output
folder.

## Development

```console
uv run python -m unittest discover -s tests -v
```
