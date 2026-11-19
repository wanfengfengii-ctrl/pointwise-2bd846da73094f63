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
    lane = binding.lane(lane_id)
    slot = lane.read_current()
    if slot is None:
        return None
    plate = validate_current_plate(binding.manifest, slot)
    handoff = accept_current_plate(evidence, slot, plate)
    token = confirm_release_eligibility(slot, handoff)
    boards = evidence.read_trigger(plate.key)
    if len(boards) != 2:
        return CaptureStep(handoff, lane.current_position, False)
    partner = next(board for board in boards if board.origin.lane is not lane_id)
    peer_lane = binding.lane(partner.origin.lane)
    peer_slot = peer_lane.read_current()
    if peer_slot is not None and peer_slot.plate == partner and peer_slot.matches_plate(partner):
        peer_lane.release_current(peer_slot.token)
    advanced = lane.release_current(token)
    return CaptureStep(handoff, advanced.next_position, advanced.exhausted)
