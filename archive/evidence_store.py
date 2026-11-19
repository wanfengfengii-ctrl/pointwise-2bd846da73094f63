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
        self._intake: dict[tuple[str, LaneId], RawPlate] = {}

    def initialize(self) -> None:
        with write_transaction(self._db):
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS plate_evidence (
                    batch_id TEXT NOT NULL,
                    trigger_id TEXT NOT NULL,
                    first_lane TEXT NOT NULL CHECK(first_lane IN ('left', 'right')),
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

    def insert_or_read(self, plate: RawPlate) -> RawPlate:
        key, origin = plate.key, plate.origin
        other_lane = LaneId.RIGHT if origin.lane is LaneId.LEFT else LaneId.LEFT
        own_slot = (key.batch_id, origin.lane)
        other_slot = (key.batch_id, other_lane)
        other = self._intake.get(other_slot)
        matched = other is not None and other.key == key
        with write_transaction(self._db):
            stored = self.read_origin(origin)
            if stored == plate:
                return stored
            if stored is not None or self.read_trigger(key):
                raise InvalidPlate("raw origin or trigger lane conflicts with stored evidence")
            buffered = self._intake.get(own_slot)
            if (buffered is not None and buffered != plate
                    and (buffered.key == key or buffered.origin == origin)):
                raise InvalidPlate("raw origin or trigger lane conflicts with intake")
            self._db.execute("""
                INSERT INTO plate_evidence(batch_id, trigger_id, first_lane, first_reading)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(batch_id, trigger_id) DO NOTHING
            """, (key.batch_id, key.trigger_id, origin.lane.value, encode_count(plate.reading)))
            if matched:
                left, right = (plate, other) if origin.lane is LaneId.LEFT else (other, plate)
                self._db.execute("""
                    UPDATE plate_evidence
                    SET left_seq = ?, left_reading = ?, right_seq = ?, right_reading = ?
                    WHERE batch_id = ? AND trigger_id = ?
                """, (encode_count(left.origin.sequence), encode_count(left.reading),
                      encode_count(right.origin.sequence), encode_count(right.reading),
                      key.batch_id, key.trigger_id))
        if matched:
            self._intake.pop(own_slot, None)
            self._intake.pop(other_slot)
        else:
            self._intake[own_slot] = plate
        return plate

    def read_origin(self, origin: PlateOrigin) -> RawPlate | None:
        is_left = origin.lane is LaneId.LEFT
        seq_column = "left_seq" if is_left else "right_seq"
        row = self._db.execute(f"""
            SELECT trigger_id, left_seq, left_reading, right_seq, right_reading
            FROM plate_evidence WHERE batch_id = ? AND {seq_column} = ?
        """, (origin.batch_id, encode_count(origin.sequence))).fetchone()
        if row is None:
            return None
        pair = _pair_from_row(TriggerKey(origin.batch_id, row[0]), row[1:])
        return pair[0 if is_left else 1]

    def read_trigger(self, key: TriggerKey) -> tuple[RawPlate, ...]:
        row = self._db.execute("""
            SELECT left_seq, left_reading, right_seq, right_reading
            FROM plate_evidence WHERE batch_id = ? AND trigger_id = ? AND left_seq IS NOT NULL
        """, (key.batch_id, key.trigger_id)).fetchone()
        return () if row is None else _pair_from_row(key, row)
