import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from archive.evidence import EvidenceLedger
from archive.release import publish
from batch.identity import TriggerKey
from batch.registration import register
from capture.control import step
from capture.resume import resume_capture
from review.checklist import checklist
from review.lookup import lookup
from review.reconciliation import reconcile_shift, reopen_shift
from source.plate import InvalidPlate, PlateOrigin, RawPlate
from tests import BATCH, LEFT, RIGHT, attach_to, claim_values, crossed_ports, take_steps, trace_values


class TestSpectralHandoff(unittest.TestCase):
    """Regressions for the crossed dual-spectrum single-slot handoff."""

    def test_first_slot_advances_and_stays_pending(self):
        with reopen_shift(":memory:") as shift:
            register(shift.catalog, BATCH, ("A", "B"))
            binding = attach_to(shift, crossed_ports())
            left, right = take_steps(shift, binding, LEFT, RIGHT)
            self.assertEqual((left.next_position, right.next_position), (1, 1))
            self.assertEqual([binding.lane(lane).current_position for lane in (LEFT, RIGHT)], [1, 1])
            # Each lane released only its own first plate; it did not steal the peer slot.
            self.assertEqual(trace_values(binding), [
                ("read", LEFT, 0, 11), ("release", LEFT, 0, 11),
                ("read", RIGHT, 0, 11), ("release", RIGHT, 0, 11),
            ])
            self.assertEqual(binding.lane(LEFT).read_current().plate.key.trigger_id, "B")
            self.assertEqual(binding.lane(RIGHT).read_current().plate.key.trigger_id, "A")
            self.assertIsNone(lookup(shift.catalog, shift.results, BATCH, "A"))
            self.assertEqual(shift.results.read_published_batch(BATCH), {})

    def test_half_plate_recovered_after_reopen(self):
        ports = crossed_ports()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "shift.sqlite3"
            with reopen_shift(path) as shift:
                register(shift.catalog, BATCH, ("A", "B"))
                binding = attach_to(shift, ports)
                shift.db.execute("""
                    CREATE TEMP TRIGGER interrupt_release AFTER UPDATE ON lane_positions
                    BEGIN SELECT RAISE(ABORT, 'release interrupted'); END
                """)
                with self.assertRaisesRegex(sqlite3.IntegrityError, "release interrupted"):
                    step(binding, shift.evidence, LEFT)
                self.assertEqual(binding.lane(LEFT).current_position, 0)
            with reopen_shift(path) as shift:
                binding = attach_to(shift, ports)
                accepted = shift.evidence.read_trigger(TriggerKey(BATCH, "A"))
                self.assertEqual([(p.origin.lane, p.origin.sequence, p.reading) for p in accepted],
                                 [(LEFT, 11, 31)])
                self.assertEqual(shift.evidence.read_plate(PlateOrigin(BATCH, LEFT, 11)), accepted[0])
                left, right = resume_capture(binding, shift.evidence)
                self.assertEqual(left.handoff.receipt.plate, accepted[0])
                # Releasing the recovered same plate must be idempotent for evidence.
                taken = step(binding, shift.evidence, LEFT)
                self.assertEqual(taken.next_position, 1)
                self.assertEqual(shift.evidence.read_trigger(TriggerKey(BATCH, "A")), accepted)
                self.assertIsNone(lookup(shift.catalog, shift.results, BATCH, "A"))

    def test_same_origin_conflict_rejected_across_reopen(self):
        ports = crossed_ports()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "shift.sqlite3"
            with reopen_shift(path) as shift:
                register(shift.catalog, BATCH, ("A", "B"))
                binding = attach_to(shift, ports)
                shift.db.execute("""
                    CREATE TEMP TRIGGER interrupt_release AFTER UPDATE ON lane_positions
                    BEGIN SELECT RAISE(ABORT, 'release interrupted'); END
                """)
                with self.assertRaises(sqlite3.IntegrityError):
                    step(binding, shift.evidence, LEFT)
            with reopen_shift(path) as shift:
                stored = shift.evidence.read_plate(PlateOrigin(BATCH, LEFT, 11))
                self.assertEqual(stored.reading, 31)
                for conflicting in (RawPlate(TriggerKey(BATCH, "A"), PlateOrigin(BATCH, LEFT, 11), 99),
                                    RawPlate(TriggerKey(BATCH, "B"), PlateOrigin(BATCH, LEFT, 11), 31)):
                    with self.subTest(plate=conflicting), self.assertRaises(InvalidPlate):
                        shift.evidence.accept_plate(conflicting)
                self.assertEqual(shift.evidence.read_plate(PlateOrigin(BATCH, LEFT, 11)), stored)

    def test_claim_after_both_boards_and_release_origin(self):
        with reopen_shift(":memory:") as shift:
            register(shift.catalog, BATCH, ("A", "B"))
            binding = attach_to(shift, crossed_ports())
            take_steps(shift, binding, LEFT, RIGHT, LEFT)
            self.assertIsNone(publish(shift.catalog, shift.evidence, shift.results, BATCH, "A"))
            take_steps(shift, binding, RIGHT)
            take_steps(shift, binding, RIGHT)
            self.assertIsNone(lookup(shift.catalog, shift.results, BATCH, "A"))
            signed = publish(shift.catalog, shift.evidence, shift.results, BATCH, "A")
            pending_b = publish(shift.catalog, shift.evidence, shift.results, BATCH, "B")
            self.assertIsNotNone(pending_b)
            issued = lookup(shift.catalog, shift.results, BATCH, "A")
            self.assertEqual(signed.result_id, issued.result_id)
            self.assertEqual(claim_values(issued), (31, 47, 16, PlateOrigin(BATCH, LEFT, 11),
                                                   PlateOrigin(BATCH, RIGHT, 18)))
            self.assertEqual(claim_values(lookup(shift.catalog, shift.results, BATCH, "B")),
                             (29, 43, 14, PlateOrigin(BATCH, LEFT, 14), PlateOrigin(BATCH, RIGHT, 11)))

    def test_failed_publication_leaves_no_draft_and_republishes(self):
        ports = crossed_ports()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "shift.sqlite3"
            with reopen_shift(path) as shift:
                register(shift.catalog, BATCH, ("A", "B"))
                binding = attach_to(shift, ports)
                take_steps(shift, binding, LEFT, RIGHT, LEFT, RIGHT)
                boards = shift.evidence.read_trigger(TriggerKey(BATCH, "A"))
                self.assertEqual(len(boards), 2)
                shift.db.execute("""
                    CREATE TEMP TRIGGER block_publication AFTER INSERT ON published_results
                    BEGIN SELECT RAISE(ABORT, 'publication blocked'); END
                """)
                with self.assertRaisesRegex(sqlite3.IntegrityError, "publication blocked"):
                    publish(shift.catalog, shift.evidence, shift.results, BATCH, "A")
                shift.db.execute("DROP TRIGGER temp.block_publication")
                self.assertIsNone(lookup(shift.catalog, shift.results, BATCH, "A"))
                self.assertEqual(shift.evidence.read_trigger(TriggerKey(BATCH, "A")), boards)
            with reopen_shift(path) as shift:
                self.assertEqual(shift.evidence.read_trigger(TriggerKey(BATCH, "A")), boards)
                reconciled = reconcile_shift(shift, BATCH)
                a, b = reconciled.results
                self.assertEqual([a.trigger_id, b.trigger_id], ["A", "B"])
                self.assertEqual(claim_values(a), (31, 47, 16, PlateOrigin(BATCH, LEFT, 11),
                                                   PlateOrigin(BATCH, RIGHT, 18)))
                self.assertEqual(claim_values(b), (29, 43, 14, PlateOrigin(BATCH, LEFT, 14),
                                                   PlateOrigin(BATCH, RIGHT, 11)))

    def test_checklist_keeps_manifest_order(self):
        with reopen_shift(":memory:") as shift:
            register(shift.catalog, BATCH, ("A", "B"))
            binding = attach_to(shift, crossed_ports())
            take_steps(shift, binding, LEFT, RIGHT, LEFT)
            publish(shift.catalog, shift.evidence, shift.results, BATCH, "B")
            entries = checklist(shift.catalog, shift.results, BATCH)
            self.assertEqual([(e.trigger_id, e.status) for e in entries],
                             [("A", "pending"), ("B", "published")])


if __name__ == "__main__":
    unittest.main()
