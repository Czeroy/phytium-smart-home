from datetime import datetime
import json
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_json_file(path, default):
    path = Path(path)
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def write_json_file(path, data):
    path = Path(path)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
