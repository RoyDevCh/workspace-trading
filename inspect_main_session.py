import json
from pathlib import Path
import importlib.util


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main() -> int:
    module_path = Path.home() / ".openclaw" / "gatekeeper" / "gatekeeper.py"
    mod = load_module("gatekeeper_mod", module_path)
    payload = {
        "index_path": str(mod.MAIN_AGENT_SESSIONS_INDEX_PATH),
        "index_exists": mod.MAIN_AGENT_SESSIONS_INDEX_PATH.exists(),
        "index_preview": "",
        "session_meta": mod.load_main_session_metadata(),
    }
    if mod.MAIN_AGENT_SESSIONS_INDEX_PATH.exists():
        payload["index_preview"] = mod.MAIN_AGENT_SESSIONS_INDEX_PATH.read_text(encoding="utf-8", errors="replace")[:500]
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
