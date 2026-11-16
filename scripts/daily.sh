#!/bin/sh
# Daily job: pull the latest flow and weather, then issue the forecast and alerts.
# Run inside the app container (or a venv) once a day after ~06:00 IST, when the
# flow forecast for the day is available.
set -eu
python -m ganga.cli migrate
python -m ganga.cli daily
python -m ganga.cli forecast
