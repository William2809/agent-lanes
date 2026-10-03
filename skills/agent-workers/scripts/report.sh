#!/bin/sh
# ABOUTME: Prints a finished worker's final report (the text after its last "tokens used" line).
# ABOUTME: Flags logs that ended without a report, such as provider or startup errors.
# Usage: report.sh <log-file> [<log-file> ...]
for log in "$@"; do
  echo "=================== $log"
  n=$(awk '/^tokens used/{n=NR} END{print n+0}' "$log")
  if [ "$n" -eq 0 ]; then
    echo "NO REPORT (still running, or failed). Last lines:"; tail -15 "$log"
  elif [ -z "$(tail -n +"$((n + 2))" "$log" | tr -d '[:space:]')" ]; then
    echo "NO REPORT (ended with an error; resume with: wk $(basename "$log" .log) -r). Last lines:"; head -n "$n" "$log" | tail -6
  else
    tail -n +"$((n + 2))" "$log"
  fi
done
