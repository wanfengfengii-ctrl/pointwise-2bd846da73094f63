import json
import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from batch.identity import LaneId
from batch.registration import register
from capture.control import step
from review.lookup import lookup
from review.reconciliation import _sample_ports, reopen_shift
from source.recording import attach


def inspect_first_slots(shift, sample):
    batch_id = sample["batch_id"]
    register(shift.catalog, batch_id, sample["triggers"])
    binding = attach(shift.catalog, shift.progress, batch_id, _sample_ports(sample))
    for lane in LaneId:
        step(binding, shift.evidence, lane)
    positions = [binding.lane(lane).current_position for lane in LaneId]
    result = lookup(shift.catalog, shift.results, batch_id, "A")
    observed = None if result is None else [result.left_reading, result.right_reading]
    return {"positions": positions, "lookup_A": observed}


def main():
    try:
        sample_path = Path(__file__).resolve().parent / "samples" / "shift.json"
        sample = json.loads(sample_path.read_text(encoding="utf-8"))
        with TemporaryDirectory(prefix="spectral-incident-") as directory:
            with reopen_shift(Path(directory) / "incident.sqlite3") as shift:
                observed = inspect_first_slots(shift, sample)
    except (OSError, sqlite3.Error, ValueError, TypeError, LookupError, RuntimeError) as error:
        print(f"SPECTRAL_INCIDENT_ERROR {error}", file=sys.stderr)
        return 2
    expected = {"positions": [1, 1], "lookup_A": None}
    if observed != expected:
        print("SPECTRAL_HANDOFF_MISMATCH " + json.dumps({"expected": expected, "observed": observed}),
              file=sys.stderr)
        return 1
    print(json.dumps(observed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
