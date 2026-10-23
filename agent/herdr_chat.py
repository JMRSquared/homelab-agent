import json
import os
import subprocess
import uuid
from pathlib import Path

REPLY_DIR = Path("/home/herdr/.local/state/homelab-bot/replies")
TIMEOUT_MS = 300_000


def enabled() -> bool:
    return bool(os.environ.get("HOMELAB_HERDR_AGENT", "").strip())


def agent_name() -> str:
    return os.environ.get("HOMELAB_HERDR_AGENT", "").strip()


def build_prompt(user_text: str, reply_path: Path) -> str:
    return (
        "A person messaged @homelab-bot on Slack. Do what they asked.\n"
        "You can inspect and change every Herdr workspace and agent with the herdr CLI. "
        "Use herdr agent list, herdr agent get, herdr agent read, and herdr agent prompt.\n"
        "If an agent is blocked, say so. "
        "Send an answer into that agent only when this message is the answer.\n"
        "When you finish, write the Slack reply as plain text to this file and nothing else:\n"
        f"{reply_path}\n\n"
        "Message:\n"
        f"{user_text}"
    )


def failure_message(stderr: str) -> str:
    try:
        payload = json.loads(stderr)
    except json.JSONDecodeError:
        payload = {}
    code = ""
    if isinstance(payload, dict):
        error = payload.get("error") or {}
        if isinstance(error, dict):
            code = str(error.get("code") or error.get("message") or "")
        elif error:
            code = str(error)
    if "blocked" in code or "blocked" in stderr:
        return (
            "I'm waiting on a question in the homelab session. "
            "Answer it in Herdr, then ask me again."
        )
    if "timeout" in code or "timeout" in stderr:
        return "That took too long and I stopped waiting. Look at the homelab session in Herdr."
    return "I couldn't reach the homelab session in Herdr. It's been logged."


def reply_from(stdout: str, stderr: str, returncode: int, reply_path: Path) -> str:
    if reply_path.exists():
        text = reply_path.read_text().strip()
        if text:
            return text
    if returncode != 0:
        return failure_message(stderr)
    if stdout.strip():
        return stdout.strip()
    return "I finished, but there was no reply to send. Look at the homelab session in Herdr."


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sudo", "-n", "-u", "herdr", "-H", "herdr", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _prepare_reply_dir() -> None:
    subprocess.run(
        ["sudo", "-n", "-u", "herdr", "-H", "mkdir", "-p", str(REPLY_DIR)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def ask(user_text: str) -> str:
    name = agent_name()
    if not name:
        return "The Herdr session for homelab-bot is not configured."
    _prepare_reply_dir()
    reply_path = REPLY_DIR / f"{uuid.uuid4().hex}.txt"
    prompt = build_prompt(user_text, reply_path)
    try:
        completed = _run(
            ["agent", "prompt", name, prompt, "--wait", "--timeout", str(TIMEOUT_MS)],
            timeout=TIMEOUT_MS / 1000 + 15,
        )
    except subprocess.TimeoutExpired:
        return "That took too long and I stopped waiting. Look at the homelab session in Herdr."
    return reply_from(completed.stdout, completed.stderr, completed.returncode, reply_path)
