<img src="assets/logo.png" width="100" height="100" align="right" />

[![License:
MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

# StravaMerger

## Overview

`StravaMerger` is a `python`-based tool for merging multiple Strava activities spread over the day into one.
Recording devices (Garmin, Strava app etc) do not support to interleave the recording of one activity with another.
For example, if you record a ride to work in the morning and a ride back home in the evening but decide to go for a run over lunch, you end with 3 activities. This tool will:
- run over your last `--activities` Strava activities (including private ones) and check for two activities starting on the same day, with the same activity type and with the startpoint of the later activity within `--distance` of the endpoint of the earlier one
- for each match, it will concatenate the two activities, preserve all data (elevation, heartrate, temperature etc.) and upload the new activities automatically to your profile
- you then get a confirmation email with links to the new activities
- Unfortunately, Strava does not allow activities to be **deleted** via the API. You will therefore receive a second email with links for activities to-be-deleted. Delete those ones and you're good to go!

## How to use this?

#### Installation
```console
python -m venv stravamerger
source stravamerger/bin/activate
pip install -r requirements.txt
```
Good to go :rocket:

#### Example

```console
python run.py -credentials secret.json -activities 21 --distance 500 -ofolder data/ -recipient name@host.domain 
```
See documentation below.


### Strava API access

If you want to use this tool, feel free to get in touch (open an issue). I wrote this for myself so far, but happy to try make this usable more easily for others. I think currently the easiest way for personal use is to create a [Strava App](https://www.strava.com/settings/api) and then create a `secret.json` file on this repo with these keys

```txt
{
    "client_id": ID from API application (six digit),
    "client_secret": Client Secret from same page,
    "access_token": Access token with scope read, write and read_all
    "refresh_token": Same scope
    "mail": the password for your sender email (should be gmail)
}
```





## Documentation
```console
python run.py --help
Usage: run.py [OPTIONS]

Options:
  -c, --credentials TEXT          Path to the JSON file with credentials.
                                  [required]
  -r, --recipient TEXT            Email address to send to-be-deleted and
                                  merged activities to.  [required]
  -s, --sender TEXT               Email address that sends the emails.
                                  [default: jannis.born@gmail.com]
  -a, --activities INTEGER        Number of recent activities to retrieve.
                                  [required]
  -o, --ofolder TEXT              Folder path to save output files.
                                  [required]
  -d, --distance FLOAT            Distance threshold for merging activities.
                                  [default: 1000.0]
  --install-completion [bash|zsh|fish|powershell|pwsh]
                                  Install completion for the specified shell.
  --show-completion [bash|zsh|fish|powershell|pwsh]
                                  Show completion for the specified shell, to
                                  copy it or customize the installation.
  --help                          Show this message and exit.
```
