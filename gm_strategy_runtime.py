from __future__ import annotations

import json
import os
import re
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping


class GmStrategyRuntimeError(RuntimeError):
    pass


_TEMPLATE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)(?:\|([a-zA-Z0-9_]+))?\s*\}\}")
_METRICS_PATTERN = re.compile(r"^OPENCLAW_METRICS=(.+)$", re.MULTILINE)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp_slug() -> str:
    return _utc_now().strftime("%Y%m%dT%H%M%S%fZ")


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return repr(value)


def _serialize_placeholder(value: Any, filter_name: str | None) -> str:
    normalized = (filter_name or "str").strip().lower()
    if normalized == "str":
        return str(value)
    if normalized == "py":
        return repr(value)
    if normalized == "json":
        return json.dumps(_json_safe(value), ensure_ascii=False)
    raise GmStrategyRuntimeError(f"Unsupported template filter: {filter_name}")


def render_template_text(template_text: str, replacements: Mapping[str, Any]) -> str:
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        filter_name = match.group(2)
        if key not in replacements:
            missing.append(key)
            return match.group(0)
        return _serialize_placeholder(replacements[key], filter_name)

    rendered = _TEMPLATE_PATTERN.sub(replace, template_text)
    if missing:
        keys = ", ".join(sorted(set(missing)))
        raise GmStrategyRuntimeError(f"Missing template placeholders: {keys}")
    return rendered


def _build_bootstrap(
    *,
    strategy_id: str,
    mode_name: str,
    token_env_name: str,
    backtest: Mapping[str, Any] | None,
) -> str:
    normalized_mode = str(mode_name or "backtest").strip().lower()
    if normalized_mode not in {"backtest", "simulation", "live"}:
        raise GmStrategyRuntimeError(f"Unsupported gm strategy mode: {mode_name}")

    gm_mode_constant = "MODE_BACKTEST" if normalized_mode == "backtest" else "MODE_LIVE"
    run_lines = [
        "if __name__ == '__main__':",
        f"    __openclaw_token = __openclaw_os.environ.get({token_env_name!r}, '').strip()",
        "    if not __openclaw_token:",
        f"        raise RuntimeError('Missing token env: {token_env_name}')",
        "    __openclaw_emit(",
        "        'OPENCLAW_RUN_START',",
        "        {",
        f"            'mode': {normalized_mode!r},",
        f"            'strategy_id': {strategy_id!r},",
        "            'pid': __openclaw_os.getpid(),",
        "            'cwd': __openclaw_os.getcwd(),",
        "            'file': __file__,",
        "        },",
        "    )",
        "    try:",
        "        __openclaw_status = run(",
        f"            strategy_id={strategy_id!r},",
        "            filename=__openclaw_os.path.basename(__file__),",
        f"            mode={gm_mode_constant},",
        "            token=__openclaw_token,",
    ]

    if normalized_mode == "backtest":
        cfg = dict(backtest or {})
        run_lines.extend(
            [
                f"            backtest_start_time={str(cfg.get('start_time') or '').strip()!r},",
                f"            backtest_end_time={str(cfg.get('end_time') or '').strip()!r},",
                f"            backtest_initial_cash={float(cfg.get('initial_cash', 1000000) or 1000000)!r},",
                f"            backtest_transaction_ratio={float(cfg.get('transaction_ratio', 1) or 1)!r},",
                f"            backtest_commission_ratio={float(cfg.get('commission_ratio', 0) or 0)!r},",
                f"            backtest_slippage_ratio={float(cfg.get('slippage_ratio', 0) or 0)!r},",
                f"            backtest_adjust={int(cfg.get('adjust', 0) or 0)!r},",
                f"            backtest_check_cache={int(cfg.get('check_cache', 1) or 0)!r},",
                f"            backtest_match_mode={int(cfg.get('match_mode', 0) or 0)!r},",
                f"            backtest_intraday={int(cfg.get('intraday', 0) or 0)!r},",
            ]
        )

    run_lines.extend(
        [
            "        )",
            "    except Exception as __openclaw_exc:",
            "        __openclaw_emit(",
            "            'OPENCLAW_RUN_EXCEPTION',",
            "            {",
            "                'error': repr(__openclaw_exc),",
            "                'traceback': __openclaw_traceback.format_exc(),",
            "            },",
            "        )",
            "        raise",
            "    else:",
            "        __openclaw_emit('OPENCLAW_RUN_EXIT', {'status': __openclaw_status})",
        ]
    )

    return "\n".join(
        [
            "",
            "# --- OpenClaw bootstrap ---",
            "import json as __openclaw_json",
            "import os as __openclaw_os",
            "import traceback as __openclaw_traceback",
            "from pathlib import Path as __OpenClawPath",
            "from gm.api import *",
            "",
            "def __openclaw_json_safe(value):",
            "    if isinstance(value, (str, int, float, bool)) or value is None:",
            "        return value",
            "    if isinstance(value, dict):",
            "        return {str(key): __openclaw_json_safe(item) for key, item in value.items()}",
            "    if isinstance(value, (list, tuple, set)):",
            "        return [__openclaw_json_safe(item) for item in value]",
            "    return repr(value)",
            "",
            "def __openclaw_emit(name, payload):",
            "    encoded = __openclaw_json.dumps(__openclaw_json_safe(payload), ensure_ascii=False)",
            "    print(f'{name}=' + encoded, flush=True)",
            "",
            "def __openclaw_capture_metrics(indicator):",
            "    payload = __openclaw_json_safe(indicator)",
            "    encoded = __openclaw_json.dumps(payload, ensure_ascii=False)",
            "    print('OPENCLAW_METRICS=' + encoded, flush=True)",
            "    metrics_path = __openclaw_os.environ.get('OPENCLAW_METRICS_PATH', '').strip()",
            "    if metrics_path:",
            "        __OpenClawPath(metrics_path).write_text(encoded, encoding='utf-8')",
            "",
            "__openclaw_user_on_backtest_finished = globals().get('on_backtest_finished')",
            "if callable(__openclaw_user_on_backtest_finished):",
            "    def on_backtest_finished(context, indicator):",
            "        __openclaw_user_on_backtest_finished(context, indicator)",
            "        __openclaw_capture_metrics(indicator)",
            "else:",
            "    def on_backtest_finished(context, indicator):",
            "        __openclaw_capture_metrics(indicator)",
            "",
            "__openclaw_emit(",
            "    'OPENCLAW_BOOTSTRAP_READY',",
            "    {",
            f"        'mode': {normalized_mode!r},",
            f"        'strategy_id': {strategy_id!r},",
            "        'pid': __openclaw_os.getpid(),",
            "        'cwd': __openclaw_os.getcwd(),",
            "        'file': __file__,",
            "    },",
            ")",
            "",
            *run_lines,
            "",
        ]
    )


def _python_command(python_executable: str, script_path: Path) -> list[str]:
    return [python_executable, "-u", str(script_path)]


def _metrics_from_text(text: str) -> Dict[str, Any] | None:
    matches = _METRICS_PATTERN.findall(text or "")
    for raw in reversed(matches):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
        return {"value": payload}
    return None


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _ensure_dir(path.parent)
    path.write_text(json.dumps(_json_safe(dict(payload)), ensure_ascii=False, indent=2), encoding="utf-8")


def _load_json_if_exists(path: Path) -> Dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(raw, dict):
        return raw
    return {"value": raw}


def _is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        line = (completed.stdout or "").strip()
        # `tasklist /FO CSV` returns a quoted CSV row when the process exists.
        # Localized "process not found" messages do not start with a quote.
        return bool(line) and line.startswith('"')
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def stop_simulation_process(pid: int, force: bool = False) -> Dict[str, Any]:
    running_before = _is_process_running(pid)
    if not running_before:
        return {"ok": True, "pid": pid, "stopped": False, "already_stopped": True}

    if os.name == "nt":
        command = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            command.append("/F")
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        running_after = _is_process_running(pid)
        missing_process = "没有找到进程" in (completed.stderr or "") or "not found" in (completed.stderr or "").lower()
        return {
            "ok": completed.returncode == 0 or not running_after or missing_process,
            "pid": pid,
            "stopped": (not running_after) or missing_process,
            "already_stopped": missing_process,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }

    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        if sig is not None:
            os.kill(pid, sig)
    except OSError as exc:
        return {"ok": False, "pid": pid, "stopped": False, "error": str(exc)}
    return {"ok": True, "pid": pid, "stopped": not _is_process_running(pid), "already_stopped": False}


def read_simulation_state(state_path: Path) -> Dict[str, Any]:
    state = _load_json_if_exists(state_path) or {}
    pid = int(state.get("pid") or 0)
    state["state_path"] = str(state_path)
    state["running"] = _is_process_running(pid)
    return state


def _rendered_script(
    *,
    template_text: str,
    params: Mapping[str, Any],
    strategy_id: str,
    mode_name: str,
    metrics_path: Path,
    token_env_name: str,
    backtest: Mapping[str, Any] | None,
    run_id: str,
) -> str:
    replacements: Dict[str, Any] = dict(params)
    replacements.setdefault("strategy_id", strategy_id)
    replacements.setdefault("openclaw_run_id", run_id)
    replacements.setdefault("openclaw_mode", mode_name)
    replacements.setdefault("openclaw_metrics_path", str(metrics_path))
    replacements.setdefault("gm_token_env", token_env_name)
    replacements.setdefault("gm_token_expr", f"os.environ.get({token_env_name!r}, '')")
    replacements.setdefault("gm_mode_constant", "MODE_BACKTEST" if mode_name == "backtest" else "MODE_LIVE")
    rendered = render_template_text(template_text, replacements)
    return rendered + _build_bootstrap(
        strategy_id=strategy_id,
        mode_name=mode_name,
        token_env_name=token_env_name,
        backtest=backtest,
    )


def run_backtest(
    *,
    template_path: Path,
    params: Mapping[str, Any],
    runtime_dir: Path,
    python_executable: str,
    token: str,
    token_env_name: str = "GM_TOKEN",
    strategy_id: str = "openclaw_gm_strategy",
    backtest: Mapping[str, Any] | None = None,
    timeout_sec: int = 300,
) -> Dict[str, Any]:
    run_id = f"backtest_{_timestamp_slug()}"
    run_dir = runtime_dir / "backtests" / run_id
    _ensure_dir(run_dir)
    metrics_path = run_dir / "metrics.json"
    script_path = run_dir / "strategy.py"
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    report_path = run_dir / "report.json"

    template_text = template_path.read_text(encoding="utf-8")
    script_path.write_text(
        _rendered_script(
            template_text=template_text,
            params=params,
            strategy_id=strategy_id,
            mode_name="backtest",
            metrics_path=metrics_path,
            token_env_name=token_env_name,
            backtest=backtest,
            run_id=run_id,
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env[token_env_name] = token
    env["OPENCLAW_METRICS_PATH"] = str(metrics_path)
    env["OPENCLAW_RUN_ID"] = run_id

    timed_out = False
    stdout = ""
    stderr = ""
    returncode = -1
    try:
        completed = subprocess.run(
            _python_command(python_executable, script_path),
            capture_output=True,
            text=True,
            timeout=max(int(timeout_sec or 300), 1),
            env=env,
            cwd=str(run_dir),
            check=False,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        returncode = int(completed.returncode)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        returncode = -9

    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")

    metrics = _load_json_if_exists(metrics_path) or _metrics_from_text(stdout) or _metrics_from_text(stderr)
    report = {
        "ok": (returncode == 0) and not timed_out,
        "mode": "backtest",
        "run_id": run_id,
        "strategy_id": strategy_id,
        "template_path": str(template_path),
        "script_path": str(script_path),
        "metrics_path": str(metrics_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "returncode": returncode,
        "timed_out": timed_out,
        "metrics": metrics,
        "params": dict(params),
        "backtest": dict(backtest or {}),
    }
    _write_json(report_path, report)
    report["report_path"] = str(report_path)
    return report


def start_simulation(
    *,
    template_path: Path,
    params: Mapping[str, Any],
    runtime_dir: Path,
    python_executable: str,
    token: str,
    state_path: Path,
    token_env_name: str = "GM_TOKEN",
    strategy_id: str = "openclaw_gm_strategy",
    replace_existing: bool = True,
) -> Dict[str, Any]:
    existing = read_simulation_state(state_path) if state_path.exists() else {}
    if existing.get("running") and replace_existing:
        stop_result = stop_simulation_process(int(existing.get("pid") or 0), force=True)
    else:
        stop_result = None

    run_id = f"simulation_{_timestamp_slug()}"
    run_dir = runtime_dir / "simulation" / run_id
    _ensure_dir(run_dir)
    metrics_path = run_dir / "metrics.json"
    script_path = run_dir / "strategy.py"
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    pid_path = state_path.with_suffix(".pid")

    template_text = template_path.read_text(encoding="utf-8")
    script_path.write_text(
        _rendered_script(
            template_text=template_text,
            params=params,
            strategy_id=strategy_id,
            mode_name="simulation",
            metrics_path=metrics_path,
            token_env_name=token_env_name,
            backtest=None,
            run_id=run_id,
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env[token_env_name] = token
    env["OPENCLAW_METRICS_PATH"] = str(metrics_path)
    env["OPENCLAW_RUN_ID"] = run_id

    stdout_handle = stdout_path.open("ab")
    stderr_handle = stderr_path.open("ab")
    try:
        popen_kwargs: Dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": stdout_handle,
            "stderr": stderr_handle,
            "env": env,
            "cwd": str(run_dir),
        }
        if os.name == "nt":
            # Break away from the SSH job/console so the strategy keeps
            # running after the bridge process exits.
            popen_kwargs["creationflags"] = 0x01000000 | 0x08000000 | 0x00000200
            # Keep redirected file handles inheritable on Windows so detached
            # background strategy logs remain visible in stdout/stderr files.
            popen_kwargs["close_fds"] = False
        else:
            popen_kwargs["start_new_session"] = True
        process = subprocess.Popen(_python_command(python_executable, script_path), **popen_kwargs)
    finally:
        stdout_handle.close()
        stderr_handle.close()

    state = {
        "ok": True,
        "mode": "simulation",
        "run_id": run_id,
        "strategy_id": strategy_id,
        "template_path": str(template_path),
        "script_path": str(script_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "metrics_path": str(metrics_path),
        "state_path": str(state_path),
        "pid_path": str(pid_path),
        "pid": process.pid,
        "params": dict(params),
        "started_at": _utc_now().isoformat(),
    }
    _write_json(state_path, state)
    pid_path.write_text(str(process.pid), encoding="utf-8")
    if stop_result is not None:
        state["replaced_existing"] = stop_result
    return state
