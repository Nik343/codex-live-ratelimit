#!/usr/bin/env python3
"""
Minimal live rate-limit checker for Codex CLI.

This script queries `codex app-server` using the CODEX_HOME profile provided
via `--input-folder` and prints spent percentages for the 5-hour and weekly
limits. Missing buckets are reported as 0.0%.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Any


INIT_REQUEST = {
    "id": 1,
    "method": "initialize",
    "params": {
        "clientInfo": {
            "name": "codex_live_ratelimit",
            "title": None,
            "version": "1.0.0",
        },
        "capabilities": {
            "experimentalApi": True,
        },
    },
}

RATE_LIMITS_REQUEST = {
    "id": 2,
    "method": "account/rateLimits/read",
}


def default_codex_home() -> Path:
    """Resolve the default CODEX_HOME path."""
    env_value = os.environ.get("CODEX_HOME")
    if env_value:
        return Path(env_value).expanduser()
    return Path.home() / ".codex"


def normalize_codex_home(raw_path: str | None) -> Path:
    """Accept either CODEX_HOME or a sessions directory and normalize it."""
    path = Path(raw_path).expanduser() if raw_path else default_codex_home()

    if path.name.lower() == "sessions" and (path.parent / "auth.json").exists():
        return path.parent

    return path


def _read_json_line(
    process: subprocess.Popen[str], timeout_seconds: float
) -> dict[str, Any]:
    """Read a single JSON object from stdout with a timeout."""
    queue: Queue[Any] = Queue()

    def reader() -> None:
        try:
            queue.put(process.stdout.readline())
        except Exception as exc:  # pragma: no cover - defensive
            queue.put(exc)

    Thread(target=reader, daemon=True).start()

    try:
        result = queue.get(timeout=timeout_seconds)
    except Empty as exc:
        raise TimeoutError(
            "Timed out waiting for codex app-server response."
        ) from exc

    if isinstance(result, Exception):
        raise result

    if result == "":
        stderr_output = process.stderr.read().strip() if process.stderr else ""
        message = "codex app-server closed without returning data."
        if stderr_output:
            message = f"{message} {stderr_output}"
        raise RuntimeError(message)

    try:
        return json.loads(result)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Received invalid JSON from codex app-server: {result!r}"
        ) from exc


def _send_json_line(process: subprocess.Popen[str], payload: dict[str, Any]) -> None:
    """Send a single JSON object to the app-server."""
    process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    process.stdin.flush()


def resolve_codex_command() -> list[str]:
    """Resolve a Windows-safe Codex CLI launcher."""
    if os.name == "nt":
        for candidate in ("codex.cmd", "codex.exe", "codex"):
            resolved = shutil.which(candidate)
            if resolved:
                return [resolved]
    else:
        resolved = shutil.which("codex")
        if resolved:
            return [resolved]

    return ["codex"]


def query_rate_limits(codex_home: Path) -> dict[str, Any]:
    """Call `codex app-server` and return the live rate-limit payload."""
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    codex_command = resolve_codex_command()

    process = subprocess.Popen(
        [*codex_command, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    try:
        if (
            process.stdin is None
            or process.stdout is None
            or process.stderr is None
        ):
            raise RuntimeError("Failed to open pipes to codex app-server.")

        _send_json_line(process, INIT_REQUEST)
        init_response = _read_json_line(process, timeout_seconds=10)
        if init_response.get("id") != 1 or "result" not in init_response:
            raise RuntimeError(f"Unexpected initialize response: {init_response}")

        _send_json_line(process, RATE_LIMITS_REQUEST)
        rate_response = _read_json_line(process, timeout_seconds=10)
        if rate_response.get("id") != 2:
            raise RuntimeError(f"Unexpected rate-limit response: {rate_response}")
        if "error" in rate_response:
            raise RuntimeError(
                f"codex app-server returned an error: {rate_response['error']}"
            )

        result = rate_response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Unexpected rate-limit payload: {rate_response}")

        return result
    finally:
        process.terminate()
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


def format_reset_time(reset_at: int | float | None) -> str:
    """Format a Unix timestamp as a local ISO 8601 string."""
    if reset_at is None:
        return "N/A"

    try:
        reset_seconds = float(reset_at)
    except (TypeError, ValueError):
        return "N/A"

    return datetime.fromtimestamp(reset_seconds, tz=timezone.utc).astimezone().isoformat(
        timespec="seconds"
    )


def classify_window(window: dict[str, Any], limits: dict[str, dict[str, Any]]) -> None:
    """Map a rate-limit window to the 5h or weekly bucket by duration."""
    if not isinstance(window, dict):
        return

    try:
        minutes = float(window.get("windowDurationMins"))
    except (TypeError, ValueError):
        return

    try:
        used_percent = float(window.get("usedPercent", 0) or 0)
    except (TypeError, ValueError):
        used_percent = 0.0

    reset_at = window.get("resetsAt")

    if 240 <= minutes <= 360:
        bucket = limits["5h"]
    elif 9000 <= minutes <= 11000:
        bucket = limits["weekly"]
    else:
        return

    if used_percent > bucket["spent"] or bucket["reset_at"] is None:
        bucket["spent"] = used_percent
        bucket["reset_at"] = reset_at


def extract_limit_info(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract 5h and weekly limit info, defaulting missing buckets to zero and N/A."""
    limits = {
        "5h": {
            "spent": 0.0,
            "reset_at": None,
        },
        "weekly": {
            "spent": 0.0,
            "reset_at": None,
        },
    }

    snapshots: list[dict[str, Any]] = []

    by_limit_id = payload.get("rateLimitsByLimitId")
    if isinstance(by_limit_id, dict):
        for snapshot in by_limit_id.values():
            if isinstance(snapshot, dict):
                snapshots.append(snapshot)

    if not snapshots:
        fallback = payload.get("rateLimits")
        if isinstance(fallback, dict):
            snapshots.append(fallback)

    for snapshot in snapshots:
        classify_window(snapshot.get("primary"), limits)
        classify_window(snapshot.get("secondary"), limits)

    return limits


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print live 5-hour and weekly spent percentages from Codex CLI."
    )
    parser.add_argument(
        "--input-folder",
        "-i",
        help="Path to CODEX_HOME (or a sessions folder inside it).",
    )
    args = parser.parse_args()

    codex_home = normalize_codex_home(args.input_folder)
    if not codex_home.exists():
        print(f"Error: path does not exist: {codex_home}", file=sys.stderr)
        return 1

    try:
        payload = query_rate_limits(codex_home)
    except FileNotFoundError:
        print("Error: `codex` was not found on PATH.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    limits = extract_limit_info(payload)
    print(f"5h spent: {limits['5h']['spent']:.1f}%")
    print(f"5h reset: {format_reset_time(limits['5h']['reset_at'])}")
    print(f"weekly spent: {limits['weekly']['spent']:.1f}%")
    print(f"weekly reset: {format_reset_time(limits['weekly']['reset_at'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
