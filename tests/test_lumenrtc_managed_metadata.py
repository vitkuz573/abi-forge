import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
IDL_PATH = REPO_ROOT / "abi" / "generated" / "lumenrtc" / "lumenrtc.idl.json"
MANAGED_PATH = REPO_ROOT / "abi" / "bindings" / "lumenrtc.managed.json"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_managed_handle_metadata_matches_idl() -> None:
    idl = load_json(IDL_PATH)
    managed = load_json(MANAGED_PATH)

    funcs = {f["name"]: f for f in idl.get("functions", [])}
    opaque = set(idl.get("header_types", {}).get("opaque_types", []))

    handles = managed.get("handles", [])
    assert isinstance(handles, list)

    seen_cs = set()
    for entry in handles:
        assert isinstance(entry, dict)
        cs_type = entry.get("cs_type")
        release = entry.get("release")
        c_handle_type = entry.get("c_handle_type")

        assert isinstance(cs_type, str) and cs_type
        assert isinstance(release, str) and release
        assert isinstance(c_handle_type, str) and c_handle_type

        assert cs_type not in seen_cs
        seen_cs.add(cs_type)

        assert c_handle_type in opaque
        assert release in funcs
        params = funcs[release].get("parameters", [])
        assert params and params[0].get("c_type") == c_handle_type
