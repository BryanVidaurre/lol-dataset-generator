"""Stream CSV snapshots from committed SQLite rows, then replace atomically."""
import csv
import os
import tempfile
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from database import db_manager


def write_csv_atomic(path, fieldnames: Sequence[str], rows: Iterable[Mapping]) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    count = 0
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", newline="", encoding="utf-8", dir=str(path.parent),
            prefix=path.name + ".", suffix=".tmp", delete=False
        ) as output:
            temporary = Path(output.name)
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({column: row.get(column, "") for column in fieldnames})
                count += 1
            output.flush()
            os.fsync(output.fileno())
        os.replace(str(temporary), str(path))
        return count
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def import_existing_dataset(path) -> int:
    """One-time, transactional import, preserving old CSV rows without HTTP."""
    path = Path(path).resolve()
    if not path.is_file() or db_manager.dataset_was_imported(str(path)):
        return 0
    with path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames or "match_id" not in reader.fieldnames:
            raise ValueError("Existing dataset.csv has no match_id header")
        return db_manager.import_dataset_rows(str(path), reader)


def export_dataset(path, fieldnames) -> int:
    return write_csv_atomic(path, fieldnames, db_manager.iter_saved_rows("match_summaries"))


def export_timelines(data_dir) -> None:
    from utils.timeline import EVENT_FIELDNAMES, MILESTONE_FIELDNAMES

    data_dir = Path(data_dir)
    write_csv_atomic(data_dir / "events.csv", EVENT_FIELDNAMES,
                     db_manager.iter_saved_rows("events"))
    write_csv_atomic(data_dir / "milestones.csv", MILESTONE_FIELDNAMES,
                     db_manager.iter_saved_rows("milestones"))
