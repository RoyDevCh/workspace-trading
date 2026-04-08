import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import importlib.util


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main() -> int:
    root = Path.home() / ".openclaw"
    gatekeeper_path = root / "gatekeeper" / "gatekeeper.py"
    mod = load_module("gatekeeper_mod", gatekeeper_path)

    approvals = mod.load_json(mod.APPROVAL_STATE_PATH)
    requests = approvals.get("requests", {}) if isinstance(approvals, dict) else {}
    request_id = "a8c38b0f"
    request = requests.get(request_id, {})

    since = datetime.now(timezone.utc) - timedelta(hours=2)
    decision, reply_text = mod.find_feishu_reply(
        request_id,
        ["ou_f2c9bcd04811050805edaa77b2de580b"],
        since,
    )

    latest_user_messages = []
    session_dir = root / "agents" / "main" / "sessions"
    for session_path in sorted(session_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]:
        try:
            lines = session_path.read_text(encoding="utf-8-sig").splitlines()
        except Exception:
            continue
        for line in reversed(lines):
            try:
                payload = json.loads(line)
            except Exception:
                continue
            message = payload.get("message") or {}
            if message.get("role") != "user":
                continue
            text = mod.extract_text_blocks(message.get("content"))
            latest_user_messages.append(
                {
                    "session": str(session_path),
                    "timestamp": payload.get("timestamp"),
                    "text": text,
                }
            )
            if len(latest_user_messages) >= 5:
                break
        if len(latest_user_messages) >= 5:
            break

    print(
        json.dumps(
            {
                "request": request,
                "decision": decision,
                "reply_text": reply_text,
                "latest_user_messages": latest_user_messages,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
