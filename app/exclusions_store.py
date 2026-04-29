from pathlib import Path
import json

EXCLUSIONS_FILE_PATH = Path(__file__).resolve().parent / "data" / "excluded_classes.json"


def load_excluded_class_ids() -> set[int]:
    if not EXCLUSIONS_FILE_PATH.exists():
        return set()

    try:
        raw_data = json.loads(EXCLUSIONS_FILE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()

    if not isinstance(raw_data, dict):
        return set()
    raw_ids = raw_data.get("excluded_class_ids", [])
    if not isinstance(raw_ids, list):
        return set()

    parsed_ids: set[int] = set()
    for raw_id in raw_ids:
        try:
            parsed_value = int(raw_id)
        except (TypeError, ValueError):
            continue
        if parsed_value > 0:
            parsed_ids.add(parsed_value)
    return parsed_ids


def save_excluded_class_ids(excluded_ids: set[int]) -> None:
    EXCLUSIONS_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"excluded_class_ids": sorted(excluded_ids)}
    EXCLUSIONS_FILE_PATH.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
