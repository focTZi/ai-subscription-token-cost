"""Find and read .json/.jsonl log files without keeping whole sessions in memory."""
import json
from pathlib import Path

LOG_SUFFIXES = (".json", ".jsonl")
INVALID = object()  # Marker for a broken JSONL line.


def find_files(paths):
    files = set()
    for path in paths:
        path = Path(path)
        if path.is_dir():
            files.update(p.resolve() for p in path.rglob("*") if p.is_file() and p.suffix in LOG_SUFFIXES)
        elif path.is_file():
            files.add(path.resolve())
    return sorted(files)


def read_values(file):
    """Yield (index, value) pairs. Broken JSONL lines yield INVALID.

    A broken .json file raises before anything is yielded, so it contributes nothing.
    """
    with open(file, encoding="utf-8") as stream:
        if file.suffix == ".json":
            value = json.load(stream)
            yield 0, value
            return
        for index, line in enumerate(stream):
            if not line.strip():
                continue
            try:
                yield index, json.loads(line)
            except ValueError:
                yield index, INVALID


def records(value):
    """Flatten arrays and {"events": [...]} / {"records": [...]} export containers."""
    if isinstance(value, list):
        for item in value:
            yield from records(item)
    elif isinstance(value, dict) and isinstance(value.get("events"), list):
        yield from records(value["events"])
    elif isinstance(value, dict) and isinstance(value.get("records"), list):
        yield from records(value["records"])
    else:
        yield value
