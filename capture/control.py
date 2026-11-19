from dataclasses import dataclass

from archive.evidence import EvidenceLedger
from batch.identity import LaneId
from capture.handoff import Handoff, accept_current_plate, confirm_release_eligibility
from capture.intake import validate_current_plate
from source.recording import RecordingBinding


@dataclass(frozen=True)
class CaptureStep:
    handoff: Handoff
    next_position: int
    exhausted: bool


def step(binding: RecordingBinding, evidence: EvidenceLedger, lane_id: LaneId) -> CaptureStep | None:
    """Hand off the current plate, then release it so this lane exposes its next slot.

    A lane has a single slot; the next plate of that lane only appears after the
    current one is released.  Acceptance and release are separate durable writes:
    a failed acceptance leaves the slot untouched, while a failed release leaves
    the accepted evidence in place so the same plate can be retried.
    """
    lane = binding.lane(lane_id)
    slot = lane.read_current()
    if slot is None:
        return None
    plate = validate_current_plate(binding.manifest, slot)
    handoff = accept_current_plate(evidence, slot, plate)
    token = confirm_release_eligibility(slot, handoff)
    advanced = lane.release_current(token)
    return CaptureStep(handoff, advanced.next_position, advanced.exhausted)
