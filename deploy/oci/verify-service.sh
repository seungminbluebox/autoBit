#!/usr/bin/env bash
set -eu
set -o pipefail
umask 077

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
source "$script_dir/libdeploy.sh"

service=autobit-paper.service
ledger=/var/lib/autobit/paper/paper.sqlite3
production_ledger=$ledger
production_data=/var/lib/autobit/raw/paper
paper_python=/opt/autobit/current/.venv/bin/python
verification_root=/var/backups/autobit/verification

initialize_evidence() {
    [ "$(id -u)" -eq 0 ] || die "service verification requires root"
    acquire_deploy_lock
    [ -L /opt/autobit/current ] || die "current release link is missing"
    release_target=$(readlink -f -- /opt/autobit/current)
    [ -n "$release_target" ] || die "current release target is unreadable"
    [ -f "$release_target/.autobit-release" ] && [ ! -L "$release_target/.autobit-release" ] \
        || die "current release metadata is invalid"
    commit=$(/usr/bin/python3 - "$release_target/.autobit-release" "$release_target" <<'PY'
import json
from pathlib import Path
import re
import sys

metadata_path, release_target = map(Path, sys.argv[1:])
metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
commit = metadata.get("commit")
if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
    raise SystemExit("release metadata commit is invalid")
if release_target != Path("/opt/autobit/releases") / commit:
    raise SystemExit("current release target does not match its metadata")
print(commit)
PY
    )
    candidate_release=$release_target
    require_literal_managed_path "$verification_root"
    require_protected_directory /var/backups/autobit 0
    if [ ! -d "$verification_root" ]; then
        [ ! -e "$verification_root" ] && [ ! -L "$verification_root" ] \
            || die "verification root is invalid"
        make_directory_nofollow "$verification_root" root root 0700
    fi
    require_protected_directory "$verification_root" 0
    utc_timestamp=$(date -u +%Y%m%dT%H%M%SZ)
    evidence=/var/backups/autobit/verification/${utc_timestamp}-${commit}
    require_literal_managed_path "$evidence"
    [ ! -e "$evidence" ] && [ ! -L "$evidence" ] \
        || die "verification evidence directory already exists: $evidence"
    make_directory_nofollow "$evidence" root root 0700
    require_protected_directory "$evidence" 0
}

write_process_command() {
    local pid="$1" output="$2"
    service_command_is_expected "$pid" \
        || die "service process command does not match the paper-only unit"
    /usr/bin/python3 - "$pid" "$output" <<'PY'
import json
from pathlib import Path
import sys

pid, output = sys.argv[1:]
raw = (Path("/proc") / pid / "cmdline").read_bytes()
if not raw.endswith(b"\0"):
    raise SystemExit("service command line is incomplete")
arguments = [item.decode("utf-8", errors="strict") for item in raw[:-1].split(b"\0")]
with Path(output).open("x", encoding="utf-8") as stream:
    json.dump(arguments, stream, ensure_ascii=True, separators=(",", ":"))
    stream.write("\n")
PY
}

write_journal_snapshot() {
    local phase="$1" journal_file="$evidence/journal-${phase}.export"
    journalctl -u "$service" --lines=1 --show-cursor --no-pager --output=export \
        > "$journal_file"
    /usr/bin/python3 - "$journal_file" "$evidence/journal-cursor-${phase}.txt" <<'PY'
from pathlib import Path
import re
import sys

journal_path, cursor_path = map(Path, sys.argv[1:])
text = journal_path.read_text(encoding="utf-8", errors="strict")
match = re.search(r"(?m)^-- cursor: (\S+)$", text)
if "__CURSOR=" not in text or match is None:
    raise SystemExit("journal snapshot does not contain an entry and cursor")
with cursor_path.open("x", encoding="utf-8") as stream:
    stream.write(match.group(1) + "\n")
PY
}

validate_service_runtime_state() {
    local active_state sub_state
    systemctl is-active --quiet "$service" \
        || die "service is not active"
    active_state=$(systemctl show "$service" --property=ActiveState --value)
    sub_state=$(systemctl show "$service" --property=SubState --value)
    [ "$active_state" = active ] \
        || die "service ActiveState is not active: $active_state"
    [ "$sub_state" = running ] \
        || die "service SubState is not running: $sub_state"
}

collect_evidence() {
    local phase="$1" pid
    validate_service_runtime_state
    date -u +%Y-%m-%dT%H:%M:%SZ > "$evidence/time-${phase}-utc.txt"
    printf '%s\n' "$release_target" > "$evidence/release-${phase}.txt"
    /usr/bin/python3 - "$evidence/boot-id-${phase}.txt" <<'PY'
from pathlib import Path
import sys

boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
with Path(sys.argv[1]).open("x", encoding="ascii") as stream:
    stream.write(boot_id + "\n")
PY
    systemctl show "$service" \
        --property=ActiveState --property=SubState --property=MainPID \
        --property=InvocationID --property=ExecMainStartTimestampMonotonic \
        > "$evidence/service-${phase}.txt"
    pid=$(systemctl show "$service" --property=MainPID --value)
    case "$pid" in ''|*[!0-9]*|0) die "service has no main process" ;; esac
    write_process_command "$pid" "$evidence/process-command-${phase}.json"
    runuser -u autobit -- env -i PATH=/usr/bin:/bin \
        HOME=/var/lib/autobit XDG_CACHE_HOME=/var/lib/autobit/.cache PYTHONDONTWRITEBYTECODE=1 \
        OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 \
        "$release_target/.venv/bin/python" -m autobit.cli paper-status --db "$ledger" \
        > "$evidence/paper-status-${phase}.json"
    runuser -u autobit -- env -i PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
        /usr/bin/python3 "$release_target/deploy/oci/sqlite_tools.py" probe --db "$ledger" \
        > "$evidence/ledger-probe-${phase}.json"
    write_journal_snapshot "$phase"
}

compare_restart_evidence() {
    /usr/bin/python3 - "$evidence" <<'PY'
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

root = Path(sys.argv[1])


def load(name):
    with (root / name).open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise SystemExit("evidence JSON must contain an object: " + name)
    return value


before_status = load("paper-status-before.json")
after_status = load("paper-status-after.json")
before_probe = load("ledger-probe-before.json")
after_probe = load("ledger-probe-after.json")


def require_type(record, name, types):
    if name not in record:
        raise SystemExit("required evidence field is missing: " + name)
    value = record[name]
    if type(value) not in types:
        raise SystemExit(name + " has an invalid type")
    return value


def require_number(record, name):
    value = require_type(record, name, (int, float))
    if not math.isfinite(value):
        raise SystemExit(name + " must be finite")
    return value


def parse_utc(name, value):
    if type(value) is not str or not value.endswith("Z"):
        raise SystemExit(name + " is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise SystemExit(name + " is invalid") from error
    if parsed.tzinfo != timezone.utc:
        raise SystemExit(name + " must be UTC")
    return parsed


for status in (before_status, after_status):
    if require_type(status, "market", (str,)) != "KRW-BTC":
        raise SystemExit("market must remain KRW-BTC")
    if require_type(status, "mode", (str,)) != "normalized-paper":
        raise SystemExit("mode must remain normalized-paper")
    candle = require_type(status, "last_completed_candle_utc", (str, type(None)))
    if candle is not None:
        parse_utc("last_completed_candle_utc", candle)
    require_number(status, "normalized_cash")
    require_number(status, "btc_quantity")
    require_type(status, "position_state", (str,))
    active_stop = require_type(status, "active_stop", (dict, type(None)))
    if active_stop is not None:
        parse_utc(
            "active_stop.active_after_utc",
            require_type(active_stop, "active_after_utc", (str,)),
        )
        require_type(active_stop, "reason", (str,))
        require_number(active_stop, "stop_price")
    pending_orders = require_type(status, "pending_orders", (list,))
    if any(type(order_id) is not str for order_id in pending_orders):
        raise SystemExit("pending_orders contains an invalid order ID")
    require_type(status, "health_stage", (str,))

for probe in (before_probe, after_probe):
    event_count = require_type(probe, "event_count", (int,))
    max_sequence = require_type(probe, "max_event_sequence", (int, type(None)))
    if event_count < 0 or (max_sequence is not None and max_sequence < 1):
        raise SystemExit("probe counts must be non-negative")
    if (event_count == 0) != (max_sequence is None):
        raise SystemExit("probe count and sequence are inconsistent")


def require_not_decreased(name, before, after):
    if before is not None and (after is None or after < before):
        raise SystemExit(name + " must not decrease")


require_not_decreased(
    "event_count", before_probe["event_count"], after_probe["event_count"]
)
require_not_decreased(
    "max_event_sequence",
    before_probe["max_event_sequence"],
    after_probe["max_event_sequence"],
)


def parse_candle(value):
    if value is None:
        return None
    return parse_utc("last_completed_candle_utc", value)


before_candle = parse_candle(before_status["last_completed_candle_utc"])
after_candle = parse_candle(after_status["last_completed_candle_utc"])
if before_candle is not None and (after_candle is None or after_candle < before_candle):
    raise SystemExit("last_completed_candle_utc must not decrease")
if before_candle == after_candle:
    stable_fields = (
        "normalized_cash",
        "btc_quantity",
        "position_state",
        "active_stop",
        "pending_orders",
        "health_stage",
    )
    for field in stable_fields:
        if before_status[field] != after_status[field]:
            raise SystemExit(field + " changed without a completed candle")
PY
    cmp --silent "$evidence/release-before.txt" "$evidence/release-after.txt" \
        || die "release target changed during service restart"
    cmp --silent "$evidence/boot-id-before.txt" "$evidence/boot-id-after.txt" \
        || die "boot ID changed during service restart"
}

verify_journal_preservation() {
    local before_cursor
    before_cursor=$(<"$evidence/journal-cursor-before.txt")
    journalctl -u "$service" --cursor "$before_cursor" --lines=1 --no-pager --output=export \
        > "$evidence/journal-before-readable-after-restart.export"
    journalctl -u "$service" --after-cursor "$before_cursor" --no-pager --output=export \
        > "$evidence/journal-after-saved-cursor.export"
    /usr/bin/python3 - "$before_cursor" \
        "$evidence/journal-before-readable-after-restart.export" \
        "$evidence/journal-after-saved-cursor.export" <<'PY'
from pathlib import Path
import re
import sys

cursor, before_path, after_path = sys.argv[1:]
before_path = Path(before_path)
after_path = Path(after_path)
before = before_path.read_text(encoding="utf-8", errors="strict")
after = after_path.read_text(encoding="utf-8", errors="strict")
if ("__CURSOR=" + cursor) not in before:
    raise SystemExit("pre-restart journal entry is no longer readable")
if "__CURSOR=" not in after:
    raise SystemExit("no journal entry exists after the saved cursor")


def invocation_id(phase):
    service_path = after_path.parent / ("service-" + phase + ".txt")
    values = []
    for line in service_path.read_text(encoding="utf-8", errors="strict").splitlines():
        if line.startswith("InvocationID="):
            values.append(line.partition("=")[2])
    if len(values) != 1 or re.fullmatch(r"[0-9A-Fa-f]{32}", values[0]) is None:
        raise SystemExit(phase + " InvocationID is missing or invalid")
    return values[0].lower()


before_invocation = invocation_id("before")
after_invocation = invocation_id("after")
if before_invocation == after_invocation:
    raise SystemExit("service restart did not create a new InvocationID")
if re.search(
    r"(?m)^_SYSTEMD_INVOCATION_ID=" + re.escape(after_invocation) + r"$", after
) is None:
    raise SystemExit("post-restart journal has no entry from the new invocation")
PY
}

run_inspect() {
    collect_evidence before
}

run_restart() {
    collect_evidence before
    systemctl restart autobit-paper.service
    # Task 5's shared validator polls the complete health contract for at most 180 seconds.
    wait_for_started_service || die "service did not satisfy restart health checks"
    collect_evidence after
    compare_restart_evidence
    verify_journal_preservation
}

dispatch_verification() {
    local mode
    [ "$#" -eq 1 ] || die "expected inspect or restart"
    mode=$1
    case "$mode" in
        inspect|restart) ;;
        *) die "expected inspect or restart" ;;
    esac

    initialize_evidence
    case "$mode" in
        inspect) run_inspect ;;
        restart) run_restart ;;
    esac
    printf 'Verification evidence: %s\n' "$evidence"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    dispatch_verification "$@"
fi
