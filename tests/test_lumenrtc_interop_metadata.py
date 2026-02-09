import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
IDL_PATH = REPO_ROOT / "abi" / "generated" / "lumenrtc" / "lumenrtc.idl.json"
META_PATH = REPO_ROOT / "abi" / "bindings" / "lumenrtc.interop.json"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_interop_metadata_covers_opaque_types() -> None:
    idl = load_json(IDL_PATH)
    meta = load_json(META_PATH)

    opaque_types = set(idl.get("header_types", {}).get("opaque_types", []))
    meta_opaque = meta.get("opaque_types", {})

    missing = sorted(opaque_types - set(meta_opaque.keys()))
    assert not missing, f"Missing opaque type metadata: {missing}"

    without_release = sorted(
        name for name, payload in meta_opaque.items() if isinstance(payload, dict) and not payload.get("release")
    )
    assert not without_release, f"Opaque types missing release: {without_release}"


def test_interop_metadata_override_targets_exist() -> None:
    idl = load_json(IDL_PATH)
    meta = load_json(META_PATH)

    structs = idl.get("header_types", {}).get("structs", {})

    overrides = meta.get("struct_field_overrides", {})
    missing = []
    for key in overrides.keys():
        if "." not in key:
            missing.append(key)
            continue
        struct_name, field_name = key.split(".", 1)
        struct = structs.get(struct_name)
        if not struct:
            missing.append(key)
            continue
        fields = {item.get("name") for item in struct.get("fields", [])}
        if field_name not in fields:
            missing.append(key)
    assert not missing, f"Invalid struct_field_overrides entries: {missing}"


def test_callback_field_overrides_reference_fields() -> None:
    idl = load_json(IDL_PATH)
    meta = load_json(META_PATH)

    structs = idl.get("header_types", {}).get("structs", {})
    callback_structs = [
        payload for name, payload in structs.items() if isinstance(name, str) and name.endswith("_callbacks_t")
    ]
    callback_fields = set()
    for struct in callback_structs:
        for field in struct.get("fields", []):
            callback_fields.add(field.get("name"))

    overrides = meta.get("callback_field_overrides", {})
    missing = sorted(name for name in overrides.keys() if name not in callback_fields)
    assert not missing, f"Invalid callback_field_overrides entries: {missing}"
