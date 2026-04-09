#!/usr/bin/env python3
"""Discord <-> OpenClaw agent bridge (v2 - robust reconnect)."""

import json
import os
import queue
import random
import re
import ssl
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ── Reconnect tuning ──────────────────────────────────────────────
RECONNECT_BASE_DELAY = 5        # initial delay in seconds
RECONNECT_MAX_DELAY = 300       # max delay: 5 minutes
RECONNECT_BACKOFF_FACTOR = 2    # exponential factor
MAX_CONSECUTIVE_FAILURES = 20   # after this many, sleep longer
LONG_SLEEP_AFTER_MAX_FAILS = 300  # 5 min pause after too many fails
HEARTBEAT_TIMEOUT = 60          # seconds without ACK = connection dead
# ──────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "")
PROXY_URL = (
    os.environ.get("HTTP_PROXY")
    or os.environ.get("HTTPS_PROXY")
    or "http://127.0.0.1:7897"
)
API_BASE = "https://discord.com/api/v10"
AGENT_TIMEOUT_SEC = int(
    str(os.environ.get("DISCORD_AGENT_TIMEOUT_SEC", "300")).strip() or "300"
)
DEFAULT_AGENT = (
    os.environ.get("DISCORD_DEFAULT_AGENT", "discord-main") or "discord-main"
).strip().lower()
DISCORD_MAIN_AGENT_ID = (
    os.environ.get("DISCORD_MAIN_AGENT_ID", "discord-main") or "discord-main"
).strip().lower()
DISCORD_MAIN_ROTATE_TOKEN_LIMIT = int(
    str(os.environ.get("DISCORD_MAIN_ROTATE_TOKEN_LIMIT", "32000")).strip() or "32000"
)

ROOT = Path.home() / ".openclaw"
WORKSPACE_BY_AGENT = {
    "discord-main": ROOT / "workspace",
    "main": ROOT / "workspace",
    "main-brain": ROOT / "workspace-main-brain",
    "content": ROOT / "workspace-content",
    "multimodal": ROOT / "workspace-multimodal",
    "monitor": ROOT / "workspace-monitor",
    "publisher": ROOT / "workspace-publisher",
    "tasks": ROOT / "workspace-tasks",
    "trading": ROOT / "workspace-trading",
}
AGENT_NAMES = set(WORKSPACE_BY_AGENT.keys())
TRADING_HINT_RE = re.compile(
    r"(btc|eth|sol|bnb|usdt|wbtc|binance|ccxt|gmtrade|ths|同花顺|掘金|A股|币安|现货|合约|仓位|风控|减仓|买入|卖出|委托|持仓|510300|openclawtest)",
    re.IGNORECASE,
)

AGENT_QUEUES = {name: queue.Queue() for name in AGENT_NAMES}
WORKER_ACTIVE = {name: threading.Event() for name in AGENT_NAMES}
SEEN_IDS = set()
SEEN_LOCK = threading.Lock()
MARKDOWN_LINK_RE = re.compile(r"!?\[([^\]]+)\]\(([^)]+)\)")
MARKDOWN_HEADER_RE = re.compile(r"(?m)^#{1,6}\s+")


def now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now_ts()}] {msg}", flush=True)


def trim_text(text: str, max_chars: int = 1900) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...(truncated)"


def sanitize_agent_text(text: str, max_chars: int = 1800) -> str:
    return trim_text((text or "").replace("\r\n", "\n").replace("\x00", "").strip(), max_chars)


def flatten_markdown_links(text: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        label = match.group(1).strip()
        target = match.group(2).strip()
        if not label or not target:
            return match.group(0)
        return f"{label}: {target}"

    return MARKDOWN_LINK_RE.sub(_replace, text or "")


def format_discord_success_text(text: str) -> str:
    formatted = sanitize_agent_text(text, 1800)
    formatted = flatten_markdown_links(formatted)
    formatted = MARKDOWN_HEADER_RE.sub("", formatted)
    return formatted.strip()


def render_agent_response(agent: str, ok: bool, result: str) -> str:
    status = "执行完成" if ok else "执行失败"
    if ok:
        body = format_discord_success_text(result) or "OK"
        return trim_text(f"**[{agent}]** {status}\n{body}", 1950)

    error_body = sanitize_agent_text(result, 1700) or "无详细输出"
    return trim_text(f"**[{agent}]** {status}\n```text\n{error_body}\n```", 1950)


def normalize_agent_name(name: str) -> str:
    token = (name or "").strip().lower().replace("_", "-")
    aliases = {
        "discordmain": "discord-main",
        "discord-main": "discord-main",
        "mainbrain": "main-brain",
        "main-brain": "main-brain",
        "main": DISCORD_MAIN_AGENT_ID,
        "content": "content",
        "multimodal": "multimodal",
        "monitor": "monitor",
        "publisher": "publisher",
        "tasks": "tasks",
        "trading": "trading",
        "trade": "trading",
    }
    return aliases.get(token, token)


def default_agent_for_message(text: str) -> str:
    preferred = normalize_agent_name(DEFAULT_AGENT)
    if preferred not in AGENT_NAMES:
        preferred = "main" if "main" in AGENT_NAMES else sorted(AGENT_NAMES)[0]
    if "trading" in AGENT_NAMES and TRADING_HINT_RE.search(text or ""):
        return "trading"
    return preferred


def parse_proxy(url: Optional[str]) -> tuple[Optional[str], Optional[int]]:
    if not url:
        return None, None
    parsed = urlparse(url)
    if parsed.hostname and parsed.port:
        return parsed.hostname, parsed.port
    return None, None


def session_store_path(agent: str) -> Path:
    return ROOT / "agents" / agent / "sessions" / "sessions.json"


def managed_session_key_for_agent(agent: str) -> Optional[str]:
    if agent == DISCORD_MAIN_AGENT_ID:
        return f"agent:{agent}:main"
    return None


def load_session_store(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"session store read failed path={path}: {exc}")
        return {}


def save_session_store(path: Path, store: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(store, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        log(f"session store write failed path={path}: {exc}")


def maybe_rotate_discord_session(agent: str) -> Optional[str]:
    session_key = managed_session_key_for_agent(agent)
    if not session_key:
        return None

    store_path = session_store_path(agent)
    store = load_session_store(store_path)
    entry = store.get(session_key)
    if not entry:
        return None

    token_count = max(
        int(entry.get("inputTokens") or 0),
        int(entry.get("totalTokens") or 0),
    )
    if token_count < DISCORD_MAIN_ROTATE_TOKEN_LIMIT:
        return None

    session_file = entry.get("sessionFile")
    store.pop(session_key, None)
    save_session_store(store_path, store)

    if session_file:
        try:
            Path(session_file).unlink(missing_ok=True)
        except Exception as exc:
            log(f"session file cleanup failed path={session_file}: {exc}")

    log(
        f"rotated discord session agent={agent} session_key={session_key} "
        f"token_count={token_count}"
    )
    return session_key


def discord_request(method: str, path: str, body: Optional[dict] = None) -> Optional[dict]:
    """Make a Discord REST API request."""
    import httpx

    headers = {
        "Authorization": f"Bot {BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    url = f"{API_BASE}{path}"

    try:
        with httpx.Client(proxy=PROXY_URL or None, timeout=15) as client:
            if method == "GET":
                response = client.get(url, headers=headers)
            elif method == "POST":
                response = client.post(url, headers=headers, json=body or {})
            elif method == "PATCH":
                response = client.patch(url, headers=headers, json=body or {})
            else:
                return None

        if response.status_code in (200, 201, 204):
            try:
                return response.json()
            except Exception:
                return {"ok": True}

        log(
            f"Discord API {method} {path} -> {response.status_code}: "
            f"{response.text[:200]}"
        )
        return None
    except Exception as exc:
        log(f"Discord API error: {exc}")
        return None


def send_discord_message(channel_id: str, content: str) -> Optional[dict]:
    content = trim_text(content, 1950)
    return discord_request(
        "POST",
        f"/channels/{channel_id}/messages",
        {"content": content},
    )


def get_gateway_url() -> str:
    result = discord_request("GET", "/gateway/bot")
    if result and "url" in result:
        return str(result["url"])
    return "wss://gateway.discord.gg"


def build_websocket_connection(url: str):
    import websocket

    ws = websocket.WebSocket(
        sslopt={"cert_reqs": ssl.CERT_NONE},
    )
    proxy_host, proxy_port = parse_proxy(PROXY_URL)
    if proxy_host and proxy_port:
        ws.connect(
            url,
            http_proxy_host=proxy_host,
            http_proxy_port=proxy_port,
            timeout=30,
        )
    else:
        ws.connect(url, timeout=30)
    return ws


def handle_message(data: dict) -> None:
    """Handle a Discord MESSAGE_CREATE event."""
    msg_id = data.get("id", "")
    author = data.get("author", {})

    if author.get("bot", False):
        return

    msg_channel = data.get("channel_id", "")
    if CHANNEL_ID and str(msg_channel) != str(CHANNEL_ID):
        return

    with SEEN_LOCK:
        if msg_id in SEEN_IDS:
            return
        SEEN_IDS.add(msg_id)
        if len(SEEN_IDS) > 1000:
            for old in list(SEEN_IDS)[:500]:
                SEEN_IDS.discard(old)

    content = (data.get("content") or "").strip()
    if not content:
        return

    username = author.get("username", "unknown")
    log(f"Message from {username}: {trim_text(content, 120)}")

    agent, body = extract_target_agent_and_task(content)
    if not agent:
        agent = default_agent_for_message(content)
        body = content

    agent_queue = AGENT_QUEUES.get(agent)
    if agent_queue is None:
        agent = default_agent_for_message(content)
        agent_queue = AGENT_QUEUES[agent]

    queued_ahead = agent_queue.qsize()
    if WORKER_ACTIVE[agent].is_set():
        queued_ahead += 1

    agent_queue.put(
        {
            "agent": agent,
            "body": body,
            "channel_id": msg_channel,
            "sender": username,
        }
    )

    if queued_ahead > 0:
        send_discord_message(
            msg_channel,
            f"`{username}` -> **{agent}** 已加入队列，前面还有 {queued_ahead} 个任务。",
        )


def extract_target_agent_and_task(text: str):
    """Extract target agent and task body from a message."""
    patterns = [
        r"^[@!](?P<agent>[A-Za-z0-9_-]+)\s+(?P<body>.+)$",
        r"^/task\s+(?P<agent>[A-Za-z0-9_-]+)\s+(?P<body>.+)$",
        r"^/ask\s+(?P<agent>[A-Za-z0-9_-]+)\s+(?P<body>.+)$",
        r"^(?P<agent>[A-Za-z0-9_-]+)\s*[:：]\s*(?P<body>.+)$",
    ]

    for pattern in patterns:
        match = re.match(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        agent = normalize_agent_name(match.group("agent"))
        if agent in AGENT_NAMES:
            return agent, match.group("body").strip()

    return None, text


def run_agent_task(agent: str, message: str):
    """Run an OpenClaw or RoyCode agent task and return the result."""
    openclaw_cmd = Path.home() / "AppData" / "Roaming" / "npm" / "openclaw.cmd"
    roycode_cmd = Path.home() / "AppData" / "Roaming" / "npm" / "roycode.cmd"
    maybe_rotate_discord_session(agent)

    if openclaw_cmd.exists():
        cmd = [
            "cmd",
            "/c",
            str(openclaw_cmd),
            "agent",
            "--agent",
            agent,
            "--message",
            message,
            "--timeout",
            str(AGENT_TIMEOUT_SEC),
        ]
    elif roycode_cmd.exists():
        workspace = WORKSPACE_BY_AGENT.get(agent, ROOT)
        cmd = [
            "cmd",
            "/c",
            str(roycode_cmd),
            "--prompt",
            message,
            "--workspace",
            str(workspace),
        ]
    else:
        return False, "no openclaw/roycode CLI found"

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=AGENT_TIMEOUT_SEC + 30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"agent timeout after {AGENT_TIMEOUT_SEC}s"
    except Exception as exc:
        return False, f"router error: {exc}"

    output = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    combined = output if not err else f"{output}\n{err}".strip()
    if proc.returncode != 0:
        return False, sanitize_agent_text(f"exit={proc.returncode}\n{combined}")
    return True, sanitize_agent_text(combined or "OK")


def worker_loop(agent_name: str) -> None:
    """Process jobs from a single agent queue."""
    agent_queue = AGENT_QUEUES[agent_name]

    while True:
        job = agent_queue.get()
        if not job:
            agent_queue.task_done()
            continue

        agent = job["agent"]
        channel_id = job["channel_id"]
        body = job["body"]
        sender = job.get("sender", "unknown")

        WORKER_ACTIVE[agent].set()
        try:
            send_discord_message(
                channel_id,
                f">>> `{sender}` -> **{agent}** 收到任务，开始执行...",
            )

            ok, result = run_agent_task(agent, body)
            response = render_agent_response(agent, ok, result)

            send_discord_message(channel_id, response)
            log(f"task {'done' if ok else 'failed'} agent={agent}")
        finally:
            WORKER_ACTIVE[agent].clear()
            agent_queue.task_done()


def run_gateway() -> None:
    """Connect to Discord Gateway with exponential backoff reconnect."""
    gateway_url = get_gateway_url()
    if not gateway_url:
        log("Failed to get gateway URL")
        return

    import websocket

    ws_url = f"{gateway_url}?v=10&encoding=json"

    session_id = None
    sequence = None
    heartbeat_interval = 41250
    consecutive_failures = 0

    while True:
        ws = None
        hb_stop = threading.Event()
        hb_ack_received = threading.Event()

        try:
            ws = build_websocket_connection(ws_url)
            log("Gateway connected!")
            consecutive_failures = 0

            def heartbeat() -> None:
                log(f"[HB] heartbeat thread started, interval={heartbeat_interval}ms")
                interval = heartbeat_interval / 1000.0
                time.sleep(interval * 0.5)
                while not hb_stop.is_set():
                    try:
                        if ws and ws.connected:
                            ws.send(json.dumps({"op": 1, "d": sequence}))
                            log("[HB] sent heartbeat")
                            hb_ack_received.clear()
                            if not hb_ack_received.wait(timeout=HEARTBEAT_TIMEOUT):
                                log(f"[HB] No ACK after {HEARTBEAT_TIMEOUT}s, connection dead")
                                try:
                                    ws.close()
                                except Exception:
                                    pass
                                hb_stop.set()
                                return
                    except Exception as exc:
                        log(f"[HB] send failed: {exc}")
                        hb_stop.set()
                        return
                    for _ in range(int(interval)):
                        if hb_stop.is_set():
                            return
                        time.sleep(1)
                log("[HB] heartbeat thread exiting")

            threading.Thread(target=heartbeat, daemon=True).start()

            event_count = 0
            while True:
                try:
                    ws.settimeout(60)
                    raw_event = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue

                if raw_event is None:
                    raise RuntimeError("gateway disconnected")

                try:
                    event = json.loads(raw_event)
                except json.JSONDecodeError:
                    continue

                event_count += 1
                op = event.get("op")
                data = event.get("d", {})
                event_type = event.get("t")
                seq = event.get("s")

                if seq is not None:
                    sequence = seq

                if event_count <= 100 or event_count % 50 == 0:
                    log(f"[EVENT #{event_count}] op={op} t={event_type} s={seq}")

                if op == 10:
                    heartbeat_interval = int(data.get("heartbeat_interval", 41250))
                    log(f"[HELLO] heartbeat_interval={heartbeat_interval}")
                    if session_id and sequence is not None:
                        ws.send(
                            json.dumps(
                                {
                                    "op": 6,
                                    "d": {
                                        "token": BOT_TOKEN,
                                        "session_id": session_id,
                                        "seq": sequence,
                                    },
                                }
                            )
                        )
                        log("Resuming session...")
                    else:
                        intents = (1 << 9) | (1 << 15)
                        ws.send(
                            json.dumps(
                                {
                                    "op": 2,
                                    "d": {
                                        "token": BOT_TOKEN,
                                        "intents": intents,
                                        "properties": {
                                            "os": "windows",
                                            "browser": "openclaw",
                                            "device": "openclaw",
                                        },
                                    },
                                }
                            )
                        )
                        log(f"Identified with Discord Gateway (intents={intents})")
                elif op == 11:
                    hb_ack_received.set()
                elif op == 7:
                    log("Gateway requested reconnect (op=7)")
                    break
                elif op == 9:
                    log(f"Invalid session (can_resume={data}), re-identifying...")
                    session_id = None
                    sequence = None
                    time.sleep(3)
                elif event_type == "READY":
                    session_id = data.get("session_id")
                    user = data.get("user", {})
                    guilds = data.get("guilds", [])
                    log(
                        f"Ready! Logged in as "
                        f"{user.get('username')}#{user.get('discriminator')}"
                    )
                    log(f"  session_id={session_id}")
                    log(
                        f"  guilds ({len(guilds)}): "
                        f"{', '.join(g.get('id', '?') for g in guilds)}"
                    )
                    send_discord_message(
                        CHANNEL_ID,
                        "OpenClaw Discord Bridge 已连接，发消息即可与 agent 对话。",
                    )
                elif event_type == "RESUMED":
                    log("Session resumed")
                elif event_type == "MESSAGE_CREATE":
                    author = data.get("author", {})
                    preview = str(data.get("content", ""))[:80]
                    log(
                        f"[MSG_CREATE] channel={data.get('channel_id')} "
                        f"author={author.get('username', '?')} "
                        f"bot={author.get('bot', False)} "
                        f"content={preview}"
                    )
                    handle_message(data)
                elif event_type and event_type not in ("READY", "RESUMED"):
                    if event_type not in ("GUILD_CREATE", "CHANNEL_CREATE", "TYPING_START"):
                        log(f"[{event_type}] (event ignored)")

            hb_stop.set()
            if ws:
                ws.close()
            log(f"WebSocket loop ended after {event_count} events")
        except websocket.WebSocketConnectionClosedException:
            log("Gateway error: Connection to remote host was lost.")
        except ssl.SSLError as e:
            log(f"Gateway SSL error: {e}")
        except ConnectionResetError:
            log("Gateway error: Connection reset by peer")
        except ConnectionRefusedError:
            log("Gateway error: Connection refused (proxy down?)")
        except OSError as e:
            log(f"Gateway OS error: {e}")
        except Exception as exc:
            log(f"Gateway error: {exc}")
            tb = traceback.format_exc()
            if len(tb) > 500:
                tb = tb[-500:]
            log(f"Traceback: {tb}")
        finally:
            hb_stop.set()
            try:
                if ws:
                    ws.close()
            except Exception:
                pass

        # ── Reconnect with exponential backoff ──
        consecutive_failures += 1
        delay = min(
            RECONNECT_BASE_DELAY * (RECONNECT_BACKOFF_FACTOR ** (consecutive_failures - 1)),
            RECONNECT_MAX_DELAY,
        )
        jitter = delay * (0.75 + random.random() * 0.5)
        delay = round(jitter, 1)

        log(
            f"Reconnecting in {delay}s... "
            f"(failures={consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})"
        )

        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            log(f"Too many consecutive failures ({consecutive_failures}), long sleep {LONG_SLEEP_AFTER_MAX_FAILS}s")
            time.sleep(LONG_SLEEP_AFTER_MAX_FAILS)
            consecutive_failures = 0
        else:
            time.sleep(delay)


def main() -> None:
    log("Discord Agent Bridge v2 starting (robust reconnect)")
    log(f"  Bot token: {BOT_TOKEN[:20]}...")
    log(f"  Channel: {CHANNEL_ID}")
    log(f"  Proxy: {PROXY_URL}")
    log(f"  Default agent: {DEFAULT_AGENT}")
    log(f"  Agents: {', '.join(sorted(AGENT_NAMES))}")
    log(f"  Reconnect: base={RECONNECT_BASE_DELAY}s max={RECONNECT_MAX_DELAY}s backoff={RECONNECT_BACKOFF_FACTOR}x")
    log(f"  Heartbeat timeout: {HEARTBEAT_TIMEOUT}s")

    result = discord_request("GET", "/users/@me")
    if result:
        log(f"  Bot user: {result.get('username', 'unknown')}")
    else:
        log("  WARNING: Discord API test failed, check proxy/token")

    for agent_name in sorted(AGENT_NAMES):
        threading.Thread(
            target=worker_loop,
            args=(agent_name,),
            daemon=True,
        ).start()

    run_gateway()


if __name__ == "__main__":
    main()
