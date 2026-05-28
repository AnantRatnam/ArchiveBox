# ruff: noqa
def write_records(records: Iterator[dict[str, Any]], stream: TextIO | None = None) -> int:
    """
    Write multiple JSONL records to stdout (or provided stream).

    Returns count of records written.
    """
    count = 0
    for record in records:
        write_record(record, stream)
        count += 1
    return count
