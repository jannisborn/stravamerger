<img src="assets/logo.png" width="100" height="100" align="right" />

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

# StravaMerger

StravaMerger automates cleanup of Strava activities. It can merge contiguous
activities, repair GPS recording holes through Google Maps, save GPX backups, upload
replacements, and email the actions that still require manual source deletion. The
hole repair is a headless integration of the useful parts of
[`gpxfix`](https://github.com/jannisborn/gpxfix).

## Behavior

Merge candidates must have the same sport, nearby endpoints, and normally the same
local date. `--require-same-gear` additionally requires the same non-empty gear.
Regardless of that flag, a repaired activity keeps its source gear and a merge keeps
the one unambiguous gear used by its sources. If sources use different gears, Strava
cannot represent both on one activity, so the daily report flags this and assigns none.

With `--fix-holes`, an unchecked track has a hole when adjacent points exceed both:

- `--hole-time-threshold` (default: 5 seconds), and
- `--hole-distance-threshold` (default: 400 m straight-line).

Google Routes supplies the missing geometry. StravaMerger interpolates timestamps and
elevation while retaining existing points and their heart-rate and temperature data.
Hole endpoints are reverse geocoded for logs and email; coordinates are used if
geocoding fails.

Travel mode comes from the Strava sport: cycle sports use `BICYCLE`; runs, walks, and
hikes use `WALK`; unsupported sports require manual review. More than
`--max-holes-per-activity` holes (default: 15), or a direct gap over 20 km, is rejected.
Hole detection ignores Strava's `AlpineSki` and `Snowboard` sport types by default,
while merge detection and automatic renaming still apply. Repeat
`--ignore-holes-for-sport` to replace that default list with other exact Strava
`sport_type` values.
If Google returns no usable route, or its route is over four times the direct gap, the
replacement uses straight-line GPX coordinates at intervals of at most three seconds.
The email says so; add `nomerge` instead of deleting the source if that repair is not
acceptable.

Generic titles are configured as case-insensitive regular expressions which must match
the complete title. The defaults cover Strava's five English periods—`Morning`,
`Lunch`, `Afternoon`, `Evening`, and `Night`—and the corresponding German titles for
runs and rides, including older compound forms such as `Mittagsradfahrt`, `Abendlauf`,
and `Nachtradfahrt`. Repeat `--generic-name-pattern` to replace the default list, for
example:

```console
--generic-name-pattern "(?:Fahrt|Lauf) am (?:Morgen|Mittag)" \
--generic-name-pattern "Lunch (?:Ride|Run)"
```

`--generic-name` remains an alias for compatibility, but its values are regular
expressions as well.

Generic activities are renamed automatically when any recorded GPX point comes within
150 m of a configured location. Coordinate rules live in `NAME_DICT` and address rules
in `ADDRESS_NAME_DICT` in `utils.py`; addresses are resolved with the configured Google
Maps key and cached in the state file. The first matching rule wins. The defaults name
tracks touching IBM as `IBM` and tracks touching `Langgrabenstrasse 32, 8105 Watt` as
`Zurich Pendeln`. Activities with a custom title, and activities whose description
contains `nomerge`, are not renamed. Successful renames appear in the same daily email.

## Fetching and persistent state

`--n_activities` is the maximum number of oldest unscreened activities handled per
run. On a fresh history, StravaMerger lists the complete summary catalog, stores only
the compact fields needed for ordering and merge detection, and starts at the oldest.
At the start of each later run it refreshes that read-only catalog, then:

1. It first refreshes unresolved deletions, uploads, reviews, and generic-name tasks.
2. It screens up to `--n_activities` oldest pending entries, stopping sooner when the
   Strava read reserve is reached.
3. It sends one report containing both unresolved and newly found work.

The program monitors
[Strava's read-limit headers](https://developers.strava.com/docs/rate-limits/), keeps
ten requests in reserve, and stops starting new checks when that reserve is reached.
Progress is recorded in `<ofolder>/stravamerger-state.json`. Once the historical
catalog is exhausted, the same unchanged daily command processes only newly discovered
activities. The summary refresh still lists the catalog so backdated uploads are not
missed, but it does not download old descriptions or GPS streams again.

Queued replacements embed their GPX data in the state file and are uploaded from
memory as gzip-compressed text. Separate GPX files are temporary recovery backups.
They and the embedded data are removed after upload or cancellation; terminal jobs are
pruned after their final notification. The compact history retains IDs, timestamps,
unresolved reminders, and pending summaries rather than full completed activities.

There is no `--force-refresh` flag. Deleting `stravamerger-state.json` starts a new
oldest-first pass. Do not delete it while replacements are pending because it contains
their upload data. State files from older versions named
`.stravamerger-state*.json` are not used by the new default path and can be removed
after any pending replacements in them have been resolved.

## Replacement workflow and email

Strava does not allow API deletion of activities. The normal flow takes two cron runs:

1. The first run prepares and persists the replacement, but does not upload it. The
   daily report asks you to delete every source or add `nomerge`.
2. After you delete the sources, the next run uploads the replacement, restores its
   gear, and includes the Strava link in that run's report.

Each run sends at most one `StravaMerger - Daily report`, combining uploads, pending
deletions, generic-name reminders, metadata results, and manual-review warnings.
Pending deletions are repeated daily. Replacement descriptions use minute-precision
timestamps; repaired tracks add a concise gap distance and endpoint location.

Deleted sources disappear from the next reminder. Add these case-insensitive markers
to a Strava activity's public description:

- `nomerge`: skip merging and hole repair, and cancel a pending replacement;
- `nofix`: skip hole repair.

Activities created by StravaMerger are excluded automatically.
Strava's API does not expose the private activity note, so markers placed there cannot
be detected.

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
  --n_activities 50 \
  --distance 800 \
  --ofolder tracks/ \
  --recipient name@example.com \
  --sender your-address@gmail.com \
  --require-same-gear \
  --fix-holes
```

Hole repair defaults to off; omit `--fix-holes` for merge-only operation. `-n 50` is a
safe daily upper bound on the default Strava tier; actual work may stop earlier after
pending actions and quota usage are accounted for. Use the same `--ofolder` or
explicit `--state` path on every cron run. Use absolute paths in cron.

Replacing an activity does not preserve kudos, comments, photos, existing segment
results, or original device attribution. Pending-job GPX backups are removed when the
job is resolved.

## Development

```console
uv run python -m unittest discover -s tests -v
```
