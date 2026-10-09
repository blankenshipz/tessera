"""Private single-owner durable journal; append before effect, never redispatch intent."""

from tessera_worker.capability_journal import CapabilityJournal

from .protocol import Assignment, Denied, Envelope, Receipt, digest, encode, open_envelope


class MonitorJournal(CapabilityJournal):
    def __init__(self, root):
        super().__init__(root)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS monitor_assignment (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    digest TEXT UNIQUE NOT NULL, document BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS monitor_receipts (
                    seq INTEGER PRIMARY KEY, digest TEXT UNIQUE NOT NULL, document BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS monitor_ack (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), seq INTEGER NOT NULL,
                    digest TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS monitor_receipts_immutable
                    BEFORE UPDATE ON monitor_receipts BEGIN SELECT RAISE(ABORT,'immutable'); END;
                CREATE TRIGGER IF NOT EXISTS monitor_receipts_retained
                    BEFORE DELETE ON monitor_receipts BEGIN SELECT RAISE(ABORT,'retained'); END;
                CREATE TRIGGER IF NOT EXISTS monitor_assignment_immutable
                    BEFORE UPDATE ON monitor_assignment BEGIN SELECT RAISE(ABORT,'immutable'); END;
                CREATE TRIGGER IF NOT EXISTS monitor_assignment_retained
                    BEFORE DELETE ON monitor_assignment BEGIN SELECT RAISE(ABORT,'retained'); END;
            """)

    def claim(self, document, assignment):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT digest,document FROM monitor_assignment").fetchone()
            if row:
                if tuple(row) != (digest(assignment), encode(document)):
                    raise Denied("journal assignment conflict")
                return False
            db.execute(
                "INSERT INTO monitor_assignment VALUES (1,?,?)",
                (digest(assignment), encode(document)),
            )
        return True

    def assignment(self, key):
        with self.connect() as db:
            row = db.execute("SELECT document FROM monitor_assignment").fetchone()
        if not row:
            raise Denied("missing assignment")
        return open_envelope(Envelope.model_validate_json(row[0]), Assignment, key, b"assignment")

    def documents(self, after=0, limit=32):
        if type(after) is not int or after < 0 or not 1 <= limit <= 32:
            raise Denied("upload bound")
        with self.connect() as db:
            rows = db.execute(
                "SELECT document FROM monitor_receipts WHERE seq>? ORDER BY seq LIMIT ?",
                (after, limit),
            ).fetchall()
        return [Envelope.model_validate_json(row[0]) for row in rows]

    def receipts(self, key):
        result, previous = [], "0" * 64
        for offset in range(0, 1024, 32):
            batch = self.documents(offset)
            for doc in batch:
                receipt = open_envelope(doc, Receipt, key, b"evidence")
                if receipt.sequence != len(result) + 1 or receipt.previous != previous:
                    raise Denied("journal chain mismatch")
                previous = digest(doc)
                result.append(receipt)
            if len(batch) < 32:
                break
        return result

    def append(self, document, receipt):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT seq,digest FROM monitor_receipts ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            if (receipt.sequence, receipt.previous) != (
                row[0] + 1 if row else 1,
                row[1] if row else "0" * 64,
            ):
                raise Denied("journal sequence conflict")
            db.execute(
                "INSERT INTO monitor_receipts VALUES (?,?,?)",
                (receipt.sequence, digest(document), encode(document)),
            )

    def acknowledge(self, sequence, receipt_digest):
        # Caller must first authenticate the assignment-bound receiver acknowledgement.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT digest FROM monitor_receipts WHERE seq=?", (sequence,)
            ).fetchone()
            old = db.execute("SELECT seq FROM monitor_ack").fetchone()
            if not row or row[0] != receipt_digest:
                raise Denied("invalid acknowledgement")
            if old and sequence <= old[0]:
                return  # authenticated historical replay cannot rewind the cursor
            db.execute(
                "INSERT OR REPLACE INTO monitor_ack VALUES (1,?,?)", (sequence, receipt_digest)
            )
