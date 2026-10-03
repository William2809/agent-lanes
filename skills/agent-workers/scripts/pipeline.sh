#!/bin/sh
# ABOUTME: Runs the Sol → Astra → Sol pipeline for one brief: Sol builds, Astra reviews the diff
# ABOUTME: read-only against the brief and the repo's standards, Sol fixes confirmed findings.
# Usage: pipeline.sh <repo-dir> <brief-file> <out-prefix> [builder-effort=preset build-hard]
# Optional PIPELINE_VERIFY: shell command run in <repo-dir> by the lead after fixes.
# Writes .build.log, .review.log, .fix.log and .done; optional .verify{,2}.{log,rc}, .fix2.{md,log}.
# .done contains ok, verify-failed, or no-verify. Run in the background.
set -u
dir=$1 brief=$2 out=$3
# Builder and reviewer come from the model presets (ao-model): roles build-hard and review.
want=${4:-}
b=$(ao-model get build-hard) || exit 2; r=$(ao-model get review) || exit 2
set -- $b; bmodel=$1 effort=${want:-$2}
set -- $r; rmodel=$1 reffort=$2
here=$(dirname "$0")
run() { "$here/run-worker.sh" "$@" && "$here/wait-workers.sh" "$6" >/dev/null; }
report() { "$here/report.sh" "$1" 2>/dev/null | tail -n +2; }
verify() {
  verify_prefix=$1
  verify_try=1
  : > "$verify_prefix.log"
  while :; do
    (cd "$dir" && sh -c "$PIPELINE_VERIFY") >> "$verify_prefix.log" 2>&1
    verify_rc=$?
    [ "$verify_rc" -eq 75 ] && [ "$verify_try" -lt 30 ] || break
    sleep 60
    verify_try=$((verify_try + 1))
  done
  printf '%s\n' "$verify_rc" > "$verify_prefix.rc"
}

run "$bmodel" "$effort" full-access "$dir" "$brief" "$out.build.log"

cat > "$out.review.md" <<REV
READ-ONLY code review. Repo: $dir. Don't edit files or run state-changing git. Read AGENTS.md and docs/engineering/coding-standards.md (including Testing) first.
Read-only git commands and remote-ci (which uses git internally) are allowed; never stage, commit, checkout, reset, or push.
The remote check runner is not reachable from your sandbox; don't try remote-ci. Rely on the build report and the lead's verify step.

A GPT Sol worker just implemented the brief below in this repo. Review its uncommitted changes (\`git status\`, \`git diff\`, and new untracked files) against:
- the brief's acceptance criteria;
- behaviour preservation (anything the brief says to keep);
- test quality: meaningful behaviour tests, no markup, snapshot or trivial tests;
- code quality: naming, responsibilities, no dumping-ground helpers. Report the line counts of touched files: new files over 500 lines, or files over 500 that grew, are findings.

Re-run the verification the brief asks for where you can (read-only). Output findings ranked High/Med/Low, each with file:line and a concrete fix, then "Looks correct" and UNVERIFIED.

----- THE BRIEF -----
$(cat "$brief")
----- THE WORKER'S REPORT -----
$(report "$out.build.log")
REV
run "$rmodel" "$reffort" read-only "$dir" "$out.review.md" "$out.review.log"

cat > "$out.fix.md" <<FIX
You implemented the brief below earlier in this repo ($dir); your changes are uncommitted. An independent read-only reviewer (Astra) reviewed them. Fix every High and Med finding, and Low ones that are cheap. If a finding is wrong, say why with evidence. Follow the same rules and verification as the brief. End with: per finding, fixed or rejected (and why); check results; UNVERIFIED.
Read-only git commands and remote-ci (which uses git internally) are allowed; never stage, commit, checkout, reset, or push.

----- REVIEW -----
$(report "$out.review.log")
----- ORIGINAL BRIEF -----
$(cat "$brief")
FIX
run "$bmodel" "$effort" full-access "$dir" "$out.fix.md" "$out.fix.log"
status=no-verify
if [ -n "${PIPELINE_VERIFY:-}" ]; then
  verify "$out.verify"
  if [ "$verify_rc" -ne 0 ] && [ "$verify_rc" -ne 75 ]; then
    cat > "$out.fix2.md" <<FIX2
The lead's verification failed after the build, review and fix stages in this repo ($dir). Fix the failure below, following the original brief's scope and rules. The lead will run verification again after this one repair round. End with: changes made; check results; UNVERIFIED.
Read-only git commands and remote-ci (which uses git internally) are allowed; never stage, commit, checkout, reset, or push.

----- VERIFICATION FAILURE (exit $verify_rc; last 150 lines) -----
$(tail -n 150 "$out.verify.log")
----- ORIGINAL BRIEF -----
$(cat "$brief")
FIX2
    run "$bmodel" "$effort" full-access "$dir" "$out.fix2.md" "$out.fix2.log"
    verify "$out.verify2"
  fi
  status=verify-failed
  [ "$verify_rc" -ne 0 ] || status=ok
fi
printf '%s\n' "$status" > "$out.done"
echo "pipeline finished: $out"
