from storage import BASE_DIR, write_json_file


FANS = ["AUTO", "LOW", "MID", "HIGH"]


def build_mapping():
    mapping = {
        "AC_ON": 1,
        "AC_OFF": 2,
        "AC_AUTO": 3,
        "AC_COOL_26_AUTO": 4,
        "AC_COOL_26": 4,
        "AC_HEAT_24_MID": 5,
        "AC_HEAT_24": 5,
        "AC_DRY_LOW": 6,
        "AC_DRY": 6,
        "AC_FAN_MID": 7,
        "AC_FAN": 7,
    }

    slot = 8
    for mode in ("COOL", "HEAT"):
        for temp in range(16, 31):
            for fan in FANS:
                code = f"AC_{mode}_{temp}_{fan}"
                if code in mapping:
                    continue
                mapping[code] = slot
                if fan == "AUTO" and f"AC_{mode}_{temp}" not in mapping:
                    mapping[f"AC_{mode}_{temp}"] = slot
                slot += 1

    for fan in FANS:
        code = f"AC_DRY_{fan}"
        if code not in mapping:
            mapping[code] = slot
            slot += 1

    for fan in FANS:
        code = f"AC_FAN_{fan}"
        if code not in mapping:
            mapping[code] = slot
            slot += 1

    return dict(sorted(mapping.items(), key=lambda item: (item[1], item[0])))


if __name__ == "__main__":
    path = BASE_DIR / "ir_forwarder_codes.json"
    mapping = build_mapping()
    write_json_file(path, mapping)
    print(f"wrote {path}")
    print(f"mapped state codes: {len(mapping)}")
    print(f"highest forwarder slot: {max(mapping.values())}")
