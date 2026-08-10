import json
import os
import re
from pathlib import Path

SNAP_DIR = Path(__file__).parent / "snapshots"
_VOLATILE_KEYS = {"id", "created_at", "modified_at", "expires_at", "iat", "exp",
                   "access_token", "token", "key", "api_key", "key_hash", "timestamp"}
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _normalize(obj):
    if isinstance(obj, dict):
        return {k: ("<VOLATILE>" if k in _VOLATILE_KEYS else _normalize(v)) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [_normalize(v) for v in obj]
    if isinstance(obj, str):
        return _UUID.sub("<UUID>", obj)
    return obj


def assert_snapshot(name: str, *, status: int, body):
    SNAP_DIR.mkdir(exist_ok=True)
    path = SNAP_DIR / f"{name}.json"
    actual = {"status": status, "body": _normalize(body)}
    if os.environ.get("CONTRACT_RECORD") == "1" or not path.exists():
        path.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
        return
    expected = json.loads(path.read_text())
    assert actual == expected, f"Contract drift for {name}:\nexpected {expected}\nactual {actual}"
