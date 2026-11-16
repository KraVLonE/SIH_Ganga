#!/bin/sh
# Nightly database dump into /backups, one file per day. Connection comes from PG* variables.
while true; do
  out="/backups/ganga-$(date +%F).dump"
  if pg_dump -Fc -f "$out"; then
    echo "backup written: $out"
  else
    echo "backup failed" >&2
  fi
  sleep 86400
done
