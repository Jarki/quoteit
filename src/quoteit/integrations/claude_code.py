"""
Claude Code integration — fetches usage via three approaches:

  Approach 1  — OAuth API (fast, ~1–2s)
  Approach 2  — Delegated refresh: expired CLI-owned token → claude refreshes
                it via PTY → retry Approach 1
  Approach 3  — CLI/PTY scraping (slow, ~5–20s)

Execution order: 1 → (2 if token expired) → 3 as last resort.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import select
import shutil
import struct
import subprocess
import sys
import termios
import time
import pty
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from quoteit.models import ExtraUsage, UsageResult, UsageWindow

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

API_BASE    = "https://api.anthropic.com"
USAGE_PATH  = "/api/oauth/usage"
BETA_HEADER = "oauth-2025-04-20"
CREDS_FILE  = Path.home() / ".claude" / ".credentials.json"


# ══════════════════════════════════════════════════════════════════════════════
# Approach 1 — OAuth API
# ══════════════════════════════════════════════════════════════════════════════

def _load_credentials() -> dict:
    if not CREDS_FILE.exists():
        raise FileNotFoundError(f"Credentials file not found: {CREDS_FILE}")

    with open(CREDS_FILE) as fh:
        data = json.load(fh)

    oauth = data.get("claudeAiOauth", {})
    if not oauth:
        raise ValueError("No 'claudeAiOauth' key in credentials file")

    expires_at_ms = oauth.get("expiresAt")
    expires_at = (
        datetime.fromtimestamp(expires_at_ms / 1000.0, tz=timezone.utc)
        if expires_at_ms is not None
        else None
    )

    return {
        "access_token":    oauth.get("accessToken"),
        "refresh_token":   oauth.get("refreshToken"),
        "expires_at":      expires_at,
        "scopes":          oauth.get("scopes", []),
        "rate_limit_tier": oauth.get("rateLimitTier"),
    }


def _infer_plan(rate_limit_tier: Optional[str]) -> Optional[str]:
    if not rate_limit_tier:
        return None
    tier = rate_limit_tier.lower()
    if "max"        in tier: return "Claude Max"
    if "pro"        in tier: return "Claude Pro"
    if "team"       in tier: return "Claude Team"
    if "enterprise" in tier: return "Claude Enterprise"
    return None


def _call_usage_api(access_token: str) -> dict:
    url = f"{API_BASE}{USAGE_PATH}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization",  f"Bearer {access_token}")
    req.add_header("Accept",         "application/json")
    req.add_header("Content-Type",   "application/json")
    req.add_header("anthropic-beta", BETA_HEADER)
    req.add_header("User-Agent",     "quoteit/1.0")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code == 401:
            raise PermissionError("OAuth token expired or invalid (401)")
        if exc.code == 403:
            raise PermissionError(f"OAuth token missing required scope (403): {body}")
        raise RuntimeError(f"API error {exc.code}: {body}") from exc


def _parse_window(raw: Optional[dict]) -> Optional[UsageWindow]:
    if not raw:
        return None
    utilization = raw.get("utilization")
    if utilization is None:
        return None
    return UsageWindow(utilization=float(utilization), resets_at=raw.get("resets_at"))


def _approach1_oauth_api() -> UsageResult:
    """Fast path: read token from disk, call HTTPS usage endpoint."""
    log.info("[1] Trying OAuth API …")


    creds = _load_credentials()

    if creds["expires_at"] and creds["expires_at"] < datetime.now(tz=timezone.utc):
        raise RuntimeError(f"Token expired at {creds['expires_at'].isoformat()}")

    if "user:profile" not in creds["scopes"]:
        raise PermissionError(
            "OAuth token missing 'user:profile' scope. "
            "Run `claude setup-token` to re-generate credentials."
        )

    raw = _call_usage_api(creds["access_token"])

    result = UsageResult(
        source="oauth_api",
        plan=_infer_plan(creds.get("rate_limit_tier")),
    )
    result.five_hour            = _parse_window(raw.get("five_hour"))
    result.seven_day            = _parse_window(raw.get("seven_day"))
    result.seven_day_oauth_apps = _parse_window(raw.get("seven_day_oauth_apps"))
    result.seven_day_opus       = _parse_window(raw.get("seven_day_opus"))
    result.seven_day_sonnet     = _parse_window(raw.get("seven_day_sonnet"))
    result.iguana_necktie       = _parse_window(raw.get("iguana_necktie"))

    eu = raw.get("extra_usage")
    if eu:
        result.extra_usage = ExtraUsage(
            is_enabled    = bool(eu.get("is_enabled", False)),
            monthly_limit = (eu.get("monthly_limit") or 0) / 100.0,
            used_credits  = (eu.get("used_credits")  or 0) / 100.0,
            utilization   = float(eu.get("utilization") or 0),
            currency      = eu.get("currency", "USD"),
        )

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Approach 3 — CLI / PTY scraping
# ══════════════════════════════════════════════════════════════════════════════

_ANSI_RE = re.compile(
    r'\x1b\[[0-9;]*[a-zA-Z]'
    r'|\x1b\][^\x07]*\x07'
    r'|\x1b.'
)

def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _run_pty(
    binary: str,
    subcommand: str,
    *,
    stop_on:           list[str]       | None = None,
    idle_timeout:      Optional[float] = None,
    send_enter_every:  Optional[float] = None,
    settle_after_stop: float           = 0.25,
    total_timeout:     float           = 30.0,
) -> str:
    master_fd, slave_fd = pty.openpty()
    winsize = struct.pack("HHHH", 40, 200, 0, 0)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

    proc = subprocess.Popen(
        [binary, subcommand],
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        start_new_session=True, close_fds=True,
    )
    os.close(slave_fd)

    stop_on = stop_on or []
    buf          = bytearray()
    t_start      = time.monotonic()
    t_last_data  = t_start
    t_last_enter = t_start
    t_stop_found = None

    try:
        while True:
            now     = time.monotonic()
            elapsed = now - t_start

            if elapsed >= total_timeout:
                break

            if t_stop_found is not None and (now - t_stop_found) >= settle_after_stop:
                break

            sel_timeout = 0.05
            if t_stop_found is not None:
                remaining   = settle_after_stop - (now - t_stop_found)
                sel_timeout = max(0.01, min(sel_timeout, remaining))

            readable, _, _ = select.select([master_fd], [], [], sel_timeout)

            if readable:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if chunk:
                    buf.extend(chunk)
                    t_last_data = now
                    if t_stop_found is None and stop_on:
                        decoded = buf.decode("utf-8", errors="replace")
                        if any(s.lower() in decoded.lower() for s in stop_on):
                            t_stop_found = now
            else:
                if idle_timeout and (now - t_last_data) >= idle_timeout:
                    break

            if send_enter_every and t_stop_found is None:
                if (now - t_last_enter) >= send_enter_every:
                    try:
                        os.write(master_fd, b"\r\n")
                        t_last_enter = now
                    except OSError:
                        break

            if proc.poll() is not None:
                try:
                    while True:
                        r, _, _ = select.select([master_fd], [], [], 0.1)
                        if not r:
                            break
                        chunk = os.read(master_fd, 4096)
                        if chunk:
                            buf.extend(chunk)
                        else:
                            break
                except OSError:
                    pass
                break
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        proc.wait(timeout=5)

    return buf.decode("utf-8", errors="replace")


# ── Parsing helpers ───────────────────────────────────────────────────────────

_PCT_RE          = re.compile(r"([0-9]{1,3}(?:\.[0-9]+)?)\s*%")
_STATUS_MODEL_RE = re.compile(r"\|.*(opus|sonnet|haiku|default).*\|", re.I)
_USED_KW         = ("used", "spent", "consumed")
_REM_KW          = ("left", "remaining", "available")


def _is_status_context_line(line: str) -> bool:
    return bool(_STATUS_MODEL_RE.search(line))


def _extract_percent_near_label(text: str, label: str) -> Optional[float]:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if label.lower() not in line.lower():
            continue
        for j in range(i, min(i + 13, len(lines))):
            candidate = lines[j]
            if _is_status_context_line(candidate):
                continue
            m = _PCT_RE.search(candidate)
            if not m:
                continue
            val      = float(m.group(1))
            cl_lower = candidate.lower()
            if any(k in cl_lower for k in _USED_KW):
                return 100.0 - val
            if any(k in cl_lower for k in _REM_KW):
                return val
    return None


def _extract_all_percents(text: str) -> list[float]:
    results = []
    for line in text.splitlines():
        if _is_status_context_line(line):
            continue
        for m in _PCT_RE.finditer(line):
            results.append(float(m.group(1)))
    return results


def _trim_to_latest_usage_panel(text: str) -> Optional[str]:
    idx = text.lower().rfind("settings:")
    if idx == -1:
        return None
    sliced = text[idx:]
    lower  = sliced.lower()
    if "usage" not in lower or "%" not in sliced:
        return None
    if not any(w in lower for w in ("used", "left", "remaining", "available", "loading usage")):
        return None
    return sliced


def _check_cli_error(text: str) -> Optional[str]:
    lower = text.lower()
    if "do you trust the files in this folder?" in lower and "current session" not in lower:
        return "Claude CLI is waiting for a folder-trust prompt. Navigate to a trusted folder."
    if "token_expired" in lower or "token has expired" in lower:
        return "Claude CLI token expired. Run `claude login` to refresh."
    if "authentication_error" in lower:
        return "Claude CLI authentication error. Run `claude login`."
    if "failed to load usage data" in lower:
        m = re.search(r"Failed to load usage data:\s*(\{.*\})", text)
        if m:
            try:
                err  = json.loads(m.group(1))
                msg  = err.get("message", "")
                code = err.get("details", {}).get("error_code", "")
                out  = f"Failed to load usage data: {msg}"
                if "token" in code.lower():
                    out += " Run `claude login` to refresh."
                return out
            except json.JSONDecodeError:
                pass
        return "Claude CLI could not load usage data."
    return None


def _parse_email(text: str) -> Optional[str]:
    for pat in (
        r"(?i)Account:\s+([^\s@]+@[^\s@]+)",
        r"(?i)Email:\s+([^\s@]+@[^\s@]+)",
        r"(?i)[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}",
    ):
        m = re.search(pat, text)
        if m:
            return m.group(1) if m.lastindex else m.group(0)
    return None


def _parse_plan(text: str) -> Optional[str]:
    m = re.search(r"(?i)login\s+method:\s*(.+)", text)
    if m:
        return m.group(1).strip()
    m = re.search(r"(?i)(claude\s+[a-z0-9][a-z0-9\s._\-]{0,24})", text)
    if m:
        candidate = m.group(1).strip()
        if "code" not in candidate.lower():
            return candidate
    return None


def _parse_reset_str(text: str) -> Optional[str]:
    m = re.search(r"Resets[^\r\n]*", text, re.IGNORECASE)
    if not m:
        return None
    line = re.sub(r"(?i)^Resets?:\s*", "", m.group(0))
    return line.strip() or None


_USAGE_STOP_STRINGS = [
    "Current week (all models)",
    "Current week (Opus)",
    "Current week (Sonnet only)",
    "Current week (Sonnet)",
    "Current session",
    "Failed to load usage data",
]


def _approach3_cli_scraping() -> UsageResult:
    """Slow path: spawn `claude /usage` + `/status` in a PTY and parse output."""
    log.info("[3] Trying CLI/PTY scraping …")


    binary = shutil.which("claude")
    if not binary:
        raise RuntimeError("`claude` binary not found on PATH")

    raw_usage = _run_pty(
        binary, "/usage",
        stop_on=_USAGE_STOP_STRINGS, idle_timeout=None,
        send_enter_every=0.8, settle_after_stop=2.0, total_timeout=30.0,
    )
    usage_text = _strip_ansi(raw_usage)

    if not any(s.lower() in usage_text.lower() for s in _USAGE_STOP_STRINGS):
        log.info("[3] Output looked like startup noise — retrying …")
        raw_usage  = _run_pty(
            binary, "/usage",
            stop_on=_USAGE_STOP_STRINGS, idle_timeout=None,
            send_enter_every=0.8, settle_after_stop=2.0, total_timeout=30.0,
        )
        usage_text = _strip_ansi(raw_usage)

    raw_status  = _run_pty(
        binary, "/status",
        stop_on=[], idle_timeout=3.0,
        send_enter_every=None, settle_after_stop=0.25, total_timeout=12.0,
    )
    status_text = _strip_ansi(raw_status)

    err = _check_cli_error(usage_text)
    if err:
        raise RuntimeError(f"CLI reported error: {err}")

    panel = _trim_to_latest_usage_panel(usage_text) or usage_text

    session_rem = _extract_percent_near_label(panel, "Current session")
    weekly_rem  = _extract_percent_near_label(panel, "Current week (all models)")
    opus_rem    = (
        _extract_percent_near_label(panel, "Current week (Opus)")
        or _extract_percent_near_label(panel, "Current week (Sonnet only)")
        or _extract_percent_near_label(panel, "Current week (Sonnet)")
    )

    has_weekly = bool(re.search(r"(?i)current week", panel))
    has_opus   = bool(re.search(r"(?i)current week \((opus|sonnet)", panel))
    all_pcts   = _extract_all_percents(panel)

    if session_rem is None and len(all_pcts) > 0:
        session_rem = all_pcts[0]
    if has_weekly and weekly_rem is None and len(all_pcts) > 1:
        weekly_rem = all_pcts[1]
    if has_opus and opus_rem is None and len(all_pcts) > 2:
        opus_rem = all_pcts[2]

    def rem_to_used(pct: Optional[float]) -> Optional[float]:
        return None if pct is None else max(0.0, min(100.0, 100.0 - pct))

    reset_str = _parse_reset_str(panel)
    combined  = usage_text + "\n" + status_text

    result = UsageResult(
        source="cli_pty",
        email=_parse_email(combined),
        plan=_parse_plan(combined),
    )

    used = rem_to_used(session_rem)
    if used is not None:
        result.five_hour = UsageWindow(utilization=used, resets_at=reset_str)

    used = rem_to_used(weekly_rem)
    if used is not None:
        result.seven_day = UsageWindow(utilization=used, resets_at=reset_str)

    used = rem_to_used(opus_rem)
    if used is not None:
        result.seven_day_opus = UsageWindow(utilization=used, resets_at=reset_str)

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Approach 2 — Delegated refresh
# ══════════════════════════════════════════════════════════════════════════════

def _approach2_delegated_refresh() -> UsageResult:
    """
    When the token has expired, run `claude /status` in a PTY to let the CLI
    refresh its own token, then reload credentials and retry Approach 1.
    """
    log.info("[2] Token expired — delegating refresh to Claude CLI …")

    binary = shutil.which("claude")
    if not binary:
        raise RuntimeError(
            "Claude CLI is not available for delegated refresh. "
            "Install/configure `claude` on your PATH."
        )

    _run_pty(
        binary, "/status",
        stop_on=[], idle_timeout=3.0,
        send_enter_every=None, settle_after_stop=0.25, total_timeout=12.0,
    )
    time.sleep(1.0)

    log.info("[2] Reloading credentials and retrying OAuth API …")
    try:
        result = _approach1_oauth_api()
        result.source = "oauth_api_after_delegated_refresh"
        return result
    except Exception as exc:
        raise RuntimeError(
            f"Token still unavailable after delegated CLI refresh: {exc}. "
            "Run `claude login`."
        ) from exc


# ══════════════════════════════════════════════════════════════════════════════
# Public entry point
# ══════════════════════════════════════════════════════════════════════════════

def fetch_usage() -> UsageResult:
    """Fetch Claude Code usage, trying approaches in order."""
    errors: list[str] = []

    try:
        return _approach1_oauth_api()
    except RuntimeError as exc:
        errors.append(f"[1] {exc}")
        if "expired" in str(exc).lower():
            try:
                return _approach2_delegated_refresh()
            except Exception as exc2:
                errors.append(f"[2] {exc2}")
    except Exception as exc:
        errors.append(f"[1] {exc}")

    try:
        return _approach3_cli_scraping()
    except Exception as exc:
        errors.append(f"[3] {exc}")

    raise RuntimeError("All approaches failed:\n" + "\n".join(f"  • {e}" for e in errors))
