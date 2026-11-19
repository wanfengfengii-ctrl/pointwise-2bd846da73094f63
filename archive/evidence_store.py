import sqlite3

from batch.catalog import write_transaction
from batch.identity import LaneId, TriggerKey
from source.plate import InvalidPlate, PlateOrigin, RawPlate, decode_count, encode_count


def _pair_from_row(key: TriggerKey, row: tuple) -> tuple[RawPlate, RawPlate]:
    left_seq, left_reading, right_seq, right_reading = row
    return (
        RawPlate(key, PlateOrigin(key.batch_id, LaneId.LEFT, decode_count(left_seq)),
                 decode_count(left_reading)),
        RawPlate(key, PlateOrigin(key.batch_id, LaneId.RIGHT, decode_count(right_seq)),
                 decode_count(right_reading)),
    )


class EvidenceStore:
    def __init__(self, db: sqlite3.Connection):
        self._db = db

    def initialize(self) -> None:
        with write_transaction(self._db):
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS plate_evidence (
                    batch_id TEXT NOT NULL,
                    trigger_id TEXT NOT NULL,
                    first_lane TEXT NOT NULL CHECK(first_lane IN ('left', 'right')),
                    first_seq TEXT,
                    first_reading TEXT NOT NULL,
                    left_seq TEXT,
                    left_reading TEXT,
                    right_seq TEXT,
                    right_reading TEXT,
                    PRIMARY KEY(batch_id, trigger_id),
                    UNIQUE(batch_id, left_seq),
                    UNIQUE(batch_id, right_seq),
                    FOREIGN KEY(batch_id, trigger_id)
                        REFERENCES manifest_triggers(batch_id, trigger_id),
                    CHECK ((left_seq IS NULL AND left_reading IS NULL
                            AND right_seq IS NULL AND right_reading IS NULL)
                           OR (left_seq IS NOT NULL AND left_reading IS NOT NULL
                               AND right_seq IS NOT NULL AND right_reading IS NOT NULL))
                )
            """)
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(plate_evidence)")}
            if "first_seq" not in columns:
                self._db.execute("ALTER TABLE plate_evidence ADD COLUMN first_seq TEXT")
            self._db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS plate_evidence_first_left_seq
                ON plate_evidence(batch_id, first_seq)
                WHERE first_lane = 'left' AND first_seq IS NOT NULL
            """)
            self._db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS plate_evidence_first_right_seq
                ON plate_evidence(batch_id, first_seq)
                WHERE first_lane = 'right' AND first_seq IS NOT NULL
            """)

    def insert_or_read(self, plate: RawPlate) -> RawPlate:
        """Durably accept one raw plate for its trigger and lane.

        The first plate of a trigger is stored on its own; the second plate of
        the other lane completes the pair in place.  Acceptance is keyed by
        batch, lane and original sequence, so repeating an identical handoff is
        idempotent while a changed reading, sequence or trigger for the same
        origin is rejected.  Everything needed to re-read a half-accepted plate
        lives in the table, not in process memory.
        """
        key, origin = plate.key, plate.origin
        lane_value = origin.lane.value
        with write_transaction(self._db):
            stored = self.read_origin(origin)
            if stored is not None:
                if stored == plate:
                    return stored
                raise InvalidPlate("raw origin or content conflicts with stored evidence")
            row = self._db.execute("""
                SELECT first_lane, first_seq, first_reading, left_seq
                FROM plate_evidence WHERE batch_id = ? AND trigger_id = ?
            """, (key.batch_id, key.trigger_id)).fetchone()
            if row is None:
                self._db.execute("""
                    INSERT INTO plate_evidence(
                        batch_id, trigger_id, first_lane, first_seq, first_reading
                    ) VALUES (?, ?, ?, ?, ?)
                """, (key.batch_id, key.trigger_id, lane_value,
                      encode_count(origin.sequence), encode_count(plate.reading)))
                return plate
            first_lane, first_seq, first_reading, left_seq = row
            if left_seq is not None:
                raise InvalidPlate("trigger already has a complete pair")
            if first_lane == lane_value:
                raise InvalidPlate("raw origin or trigger lane conflicts with stored evidence")
            first = RawPlate(
                key,
                PlateOrigin(key.batch_id, LaneId(first_lane), decode_count(first_seq)),
                decode_count(first_reading),
            )
            left, right = (plate, first) if origin.lane is LaneId.LEFT else (first, plate)
            self._db.execute("""
                UPDATE plate_evidence
                SET left_seq = ?, left_reading = ?, right_seq = ?, right_reading = ?
                WHERE batch_id = ? AND trigger_id = ?
            """, (encode_count(left.origin.sequence), encode_count(left.reading),
                  encode_count(right.origin.sequence), encode_count(right.reading),
                  key.batch_id, key.trigger_id))
        return plate

    def read_origin(self, origin: PlateOrigin) -> RawPlate | None:
        is_left = origin.lane is LaneId.LEFT
        seq_column = "left_seq" if is_left else "right_seq"
        sequence = encode_count(origin.sequence)
        paired = self._db.execute(f"""
            SELECT trigger_id, left_seq, left_reading, right_seq, right_reading
            FROM plate_evidence WHERE batch_id = ? AND {seq_column} = ?
        """, (origin.batch_id, sequence)).fetchone()
        if paired is not None:
            key = TriggerKey(origin.batch_id, paired[0])
            return _pair_from_row(key, paired[1:5])[0 if is_left else 1]
        half = self._db.execute("""
            SELECT trigger_id, first_lane, first_seq, first_reading
            FROM plate_evidence
            WHERE batch_id = ? AND left_seq IS NULL AND first_lane = ? AND first_seq = ?
        """, (origin.batch_id, origin.lane.value, sequence)).fetchone()
        if half is None:
            return None
        return RawPlate(
            TriggerKey(origin.batch_id, half[0]),
            PlateOrigin(origin.batch_id, LaneId(half[1]), decode_count(half[2])),
            decode_count(half[3]),
        )

    def read_trigger(self, key: TriggerKey) -> tuple[RawPlate, ...]:
        """Return this trigger's accepted plates in lane order (one while half-accepted)."""
        row = self._db.execute("""
            SELECT left_seq, left_reading, right_seq, right_reading,
                   first_lane, first_seq, first_reading
            FROM plate_evidence WHERE batch_id = ? AND trigger_id = ?
        """, (key.batch_id, key.trigger_id)).fetchone()
        if row is None:
            return ()
        if row[0] is not None:
            return _pair_from_row(key, row[0:4])
        if row[5] is None:
            return ()
        return (
            RawPlate(key, PlateOrigin(key.batch_id, LaneId(row[4]), decode_count(row[5])),
                     decode_count(row[6])),
        )
