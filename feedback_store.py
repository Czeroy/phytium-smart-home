from storage import BASE_DIR, now_iso, read_json_file, write_json_file


FEEDBACK_PATH = BASE_DIR / "data" / "feedback.json"
FEEDBACK_TRAINING_PATH = BASE_DIR / "data" / "feedback_training.json"
FEEDBACK_LIMIT = 300


def normalize_list(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    for separator in ("?", "？", ";", "；"):
        text = text.replace(separator, ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def result_has_training_data(result):
    if not isinstance(result, dict):
        return False
    return bool(normalize_list(result.get("intents")) or normalize_list(result.get("commands")))


def feedback_training_ready(verdict, result, expected_intents, expected_commands):
    if verdict == "correct":
        return result_has_training_data(result) or bool(expected_intents or expected_commands)
    return bool(expected_intents or expected_commands)


def normalize_feedback_item(item, fallback_id=0):
    if not isinstance(item, dict):
        return None
    result = item.get("result", {})
    if not isinstance(result, dict):
        result = {}

    try:
        item_id = int(item.get("id", fallback_id))
    except (TypeError, ValueError):
        item_id = fallback_id

    verdict = item.get("verdict", "wrong")
    if verdict not in {"correct", "wrong"}:
        verdict = "wrong"

    expected_intents = normalize_list(item.get("expected_intents"))
    expected_commands = normalize_list(item.get("expected_commands"))
    training_ready = feedback_training_ready(verdict, result, expected_intents, expected_commands)
    return {
        "id": item_id,
        "time": item.get("time") or now_iso(),
        "text": str(item.get("text", "")).strip(),
        "verdict": verdict,
        "note": str(item.get("note") or "").strip(),
        "result": result,
        "expected_intents": expected_intents,
        "expected_commands": expected_commands,
        "training_ready": training_ready,
    }


def feedback_key(item):
    result = item.get("result", {}) if isinstance(item, dict) else {}
    return (
        str(item.get("text", "")).strip(),
        str(item.get("verdict", "wrong")),
        tuple(sorted(normalize_list(item.get("expected_intents")))),
        tuple(sorted(normalize_list(item.get("expected_commands")))),
        tuple(sorted(normalize_list(result.get("intents")) if isinstance(result, dict) else [])),
        tuple(sorted(normalize_list(result.get("commands")) if isinstance(result, dict) else [])),
    )


def load_feedback():
    data = read_json_file(FEEDBACK_PATH, [])
    if isinstance(data, dict):
        data = data.get("items", [])
    if not isinstance(data, list):
        return []
    items = []
    for index, item in enumerate(data[-FEEDBACK_LIMIT:], start=1):
        normalized = normalize_feedback_item(item, fallback_id=index)
        if normalized and normalized["text"]:
            items.append(normalized)
    return items[-FEEDBACK_LIMIT:]


def save_feedback(items):
    write_json_file(FEEDBACK_PATH, {
        "updated_at": now_iso(),
        "items": items[-FEEDBACK_LIMIT:],
    })


def next_feedback_id(items):
    return max([int(item.get("id", 0) or 0) for item in items], default=0) + 1


def add_feedback(text, result=None, verdict="wrong", note="", expected_intents=None, expected_commands=None):
    items = load_feedback()
    draft = normalize_feedback_item({
        "id": next_feedback_id(items),
        "time": now_iso(),
        "text": str(text or "").strip(),
        "verdict": verdict if verdict in {"correct", "wrong"} else "wrong",
        "note": str(note or "").strip(),
        "result": result or {},
        "expected_intents": expected_intents,
        "expected_commands": expected_commands,
    })
    new_key = feedback_key(draft)
    for item in reversed(items):
        if feedback_key(item) != new_key:
            continue
        if note:
            item["note"] = str(note).strip()
        item["time"] = now_iso()
        item["training_ready"] = feedback_training_ready(
            item.get("verdict", "wrong"),
            item.get("result", {}),
            item.get("expected_intents", []),
            item.get("expected_commands", []),
        )
        save_feedback(items)
        return item

    item = draft
    items.append(item)
    save_feedback(items)
    return item


def update_feedback(feedback_id, data):
    items = load_feedback()
    updated = None
    for item in items:
        if int(item.get("id", 0)) != int(feedback_id):
            continue
        if "verdict" in data and data["verdict"] in {"correct", "wrong"}:
            item["verdict"] = data["verdict"]
        if "note" in data:
            item["note"] = str(data.get("note") or "").strip()
        if "expected_intents" in data:
            item["expected_intents"] = normalize_list(data.get("expected_intents"))
        if "expected_commands" in data:
            item["expected_commands"] = normalize_list(data.get("expected_commands"))
        item["training_ready"] = feedback_training_ready(
            item.get("verdict", "wrong"),
            item.get("result", {}),
            item.get("expected_intents", []),
            item.get("expected_commands", []),
        )
        updated = dict(item)
        break
    if updated:
        save_feedback(items)
    return updated


def recent_feedback(limit=50):
    limit = max(1, min(int(limit), FEEDBACK_LIMIT))
    return list(reversed(load_feedback()[-limit:]))


def feedback_count():
    return len(load_feedback())


def feedback_summary():
    items = load_feedback()
    wrong = [item for item in items if item["verdict"] == "wrong"]
    ready = [item for item in items if item.get("training_ready")]
    seen = set()
    duplicates = 0
    for item in items:
        key = feedback_key(item)
        if key in seen:
            duplicates += 1
        else:
            seen.add(key)
    return {
        "total": len(items),
        "correct": len([item for item in items if item["verdict"] == "correct"]),
        "wrong": len(wrong),
        "training_ready": len(ready),
        "duplicates": duplicates,
        "needs_correction": len([
            item for item in wrong
            if not item.get("expected_intents") and not item.get("expected_commands")
        ]),
    }


def deduplicate_feedback():
    items = load_feedback()
    seen = {}
    unique = []
    removed = 0
    for item in items:
        key = feedback_key(item)
        if key in seen:
            removed += 1
            existing = seen[key]
            if item.get("note") and not existing.get("note"):
                existing["note"] = item["note"]
            existing["time"] = max(str(existing.get("time", "")), str(item.get("time", "")))
            continue
        item["training_ready"] = feedback_training_ready(
            item.get("verdict", "wrong"),
            item.get("result", {}),
            item.get("expected_intents", []),
            item.get("expected_commands", []),
        )
        seen[key] = item
        unique.append(item)
    if removed:
        save_feedback(unique)
    return {
        "removed": removed,
        "items": recent_feedback(50),
        "summary": feedback_summary(),
    }


def generate_training_data():
    samples = []
    for item in load_feedback():
        result = item.get("result", {})
        if item["verdict"] == "correct":
            intents = normalize_list(item.get("expected_intents")) or normalize_list(result.get("intents"))
            commands = normalize_list(item.get("expected_commands")) or normalize_list(result.get("commands"))
        else:
            intents = normalize_list(item.get("expected_intents"))
            commands = normalize_list(item.get("expected_commands"))

        if not intents and not commands:
            continue

        samples.append({
            "text": item["text"],
            "intents": intents,
            "commands": commands,
            "note": item.get("note", ""),
            "source_feedback_id": item["id"],
            "source_time": item["time"],
        })

    payload = {
        "updated_at": now_iso(),
        "count": len(samples),
        "samples": samples,
    }
    write_json_file(FEEDBACK_TRAINING_PATH, payload)
    return payload
