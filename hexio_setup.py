#!/usr/bin/env python3
"""
Automated Hexio C2 Framework installer for cloud-init.

Drives the interactive install.sh and hexioteamserver setup wizard
using pexpect so it can run unattended.

Usage (cloud-init runcmd) — fetch license from API:
  python3 /opt/teamserver/hexio_setup.py \
      --license-url https://license-server:8443/api/license \
      --license-token "BEARER_TOKEN_HERE" \
      --root-password "ChangeMeNow!" \
      --cleanup

Or provide the blob directly:
  python3 /opt/teamserver/hexio_setup.py \
      --license-blob "BLOB_STRING" \
      --root-password "ChangeMeNow!"

Environment variables work too:
  HEXIO_LICENSE_URL, HEXIO_LICENSE_TOKEN,
  HEXIO_LICENSE_BLOB, HEXIO_ROOT_PASSWORD

SSL certificate fields default to the built-in defaults (press Enter).
Override any of them with --ssl-* flags (see --help).
"""

import argparse
import json
import logging
import os
import ssl
import subprocess
import sys
import time
import urllib.request
import urllib.error

try:
    import pexpect
except ImportError:
    print("[*] pexpect not found, installing...")
    import subprocess
    subprocess.check_call([
        sys.executable, "-m", "pip", "install",
        "--break-system-packages", "pexpect",
    ])
    import pexpect

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [hexio-setup] %(message)s",
)
log = logging.getLogger(__name__)

TEAMSERVER_DIR = "/opt/hexio"
INSTALL_SCRIPT = os.path.join(TEAMSERVER_DIR, "install.sh")
TEAMSERVER_BIN = os.path.join(TEAMSERVER_DIR, "hexioteamserver")

# Generous timeout — container image loads can be slow
DEFAULT_TIMEOUT = 86400


def fetch_license_blob(url: str, token: str, retries: int = 3, backoff: float = 5.0) -> str:
    """Fetch the license blob from a remote API endpoint with bearer-token auth."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", "hexio-setup/1.0")

    ctx = _make_ssl_ctx()

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            log.info("Fetching license from %s (attempt %d/%d) ...", url, attempt, retries)
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                body = json.loads(resp.read().decode())
                blob = body.get("license_blob", "")
                if not blob:
                    raise ValueError("Response JSON missing 'license_blob' key")
                log.info("License blob retrieved successfully (%d chars)", len(blob))
                return blob
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
            last_err = exc
            log.warning("Attempt %d failed: %s", attempt, exc)
            if attempt < retries:
                log.info("Retrying in %.0fs ...", backoff)
                time.sleep(backoff)

    log.error("Failed to fetch license after %d attempts", retries)
    raise last_err


def _make_ssl_ctx():
    """Shared SSL context that skips cert verification."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def acquire_queue_slot(queue_url: str, token: str, hostname: str,
                       poll_interval: float = 30.0) -> str:
    """Block until a queue slot is granted. Returns the slot_id."""
    url = queue_url.rstrip("/") + "/acquire"
    ctx = _make_ssl_ctx()

    while True:
        payload = json.dumps({"hostname": hostname}).encode()
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "hexio-setup/1.0")

        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            body = json.loads(resp.read().decode())

        if body["status"] == "granted":
            log.info("Queue slot granted (slot_id=%s, %d/%d active)",
                     body["slot_id"][:8], body["active"], body["max"])
            return body["slot_id"]

        retry = body.get("retry_after", poll_interval)
        log.info("Queue full (%d/%d active) — retrying in %ds ...",
                 body["active"], body["max"], retry)
        time.sleep(retry)


def release_queue_slot(queue_url: str, token: str, slot_id: str,
                       result: str, elapsed: int) -> None:
    """Release a queue slot and report result."""
    url = queue_url.rstrip("/") + "/release"
    ctx = _make_ssl_ctx()

    payload = json.dumps({
        "slot_id": slot_id,
        "result": result,
        "elapsed": elapsed,
    }).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "hexio-setup/1.0")

    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            body = json.loads(resp.read().decode())
        log.info("Queue slot released — %d/%d active", body["active"], body["max"])
    except Exception as exc:
        log.warning("Failed to release queue slot: %s", exc)


def run_install_sh(cleanup: bool, timeout: int, quiet: bool = False) -> None:
    """Run install.sh and answer its interactive prompts."""
    log.info("Running install.sh ...")
    child = pexpect.spawn(
        "/bin/bash",
        [INSTALL_SCRIPT],
        cwd=TEAMSERVER_DIR,
        timeout=timeout,
        encoding="utf-8",
        logfile=None if quiet else sys.stdout,
    )

    cleanup_answer = "y" if cleanup else "n"

    # Prompt 1: cleanup compiler scripts
    child.expect(r"cleanup the compiler scripts.*\(y/n\)")
    child.sendline(cleanup_answer)
    log.info("Answered compiler-scripts cleanup: %s", cleanup_answer)

    # Prompt 2: cleanup container tarballs
    child.expect(r"cleanup the container.*\(y/n\)")
    child.sendline(cleanup_answer)
    log.info("Answered container cleanup: %s", cleanup_answer)

    child.expect(pexpect.EOF)
    child.close()

    if child.exitstatus != 0:
        log.warning("install.sh exited with status %s (may be non-fatal)", child.exitstatus)
    else:
        log.info("install.sh completed successfully")


def run_teamserver_setup(
    license_blob: str,
    root_password: str,
    ssl_fields: dict,
    timeout: int,
    quiet: bool = False,
) -> None:
    """Run hexioteamserver for the first time and walk through the setup wizard."""
    log.info("Running hexioteamserver setup wizard ...")
    child = pexpect.spawn(
        TEAMSERVER_BIN,
        cwd=TEAMSERVER_DIR,
        timeout=timeout,
        encoding="utf-8",
        logfile=None if quiet else sys.stdout,
    )

    # --- License activation ---
    child.expect(r"Paste your license blob")
    child.sendline(license_blob)
    log.info("Submitted license blob")

    # Check for activation result
    idx = child.expect([r"License activated successfully", r"(?i)invalid|error|fail"])
    if idx != 0:
        log.error("License activation failed!")
        child.close()
        sys.exit(1)
    log.info("License activated")

    # --- Base SSL Certificate Wizard ---
    log.info("Filling base SSL certificate fields ...")
    _fill_ssl_wizard(child, ssl_fields, prefix="base")

    # --- Teamserver SSL Certificate Wizard ---
    log.info("Filling teamserver SSL certificate fields ...")
    _fill_ssl_wizard(child, ssl_fields, prefix="teamserver")

    # --- Database initialization ---
    child.expect(r"Type 'YES' to confirm")
    child.sendline("YES")
    log.info("Confirmed database initialization")

    # --- Root password ---
    child.expect(r"[Ee]nter.*root.*password")
    child.sendline(root_password)
    log.info("Entered root password")

    child.expect(r"[Cc]onfirm.*password")
    child.sendline(root_password)
    log.info("Confirmed root password")

    # Wait for setup to finish
    child.expect([r"Setup complete", r"Restart to launch", pexpect.EOF])
    log.info("Teamserver setup wizard complete")

    child.close()


def _fill_ssl_wizard(child: pexpect.spawn, ssl_fields: dict, prefix: str) -> None:
    """Answer the 6 SSL certificate prompts. Empty string = accept default."""
    field_order = [
        ("country", r"Country \(C\)"),
        ("state", r"State \(ST\)"),
        ("city", r"City \(L\)"),
        ("org", r"Organization \(O\)"),
        ("ou", r"Organizational Unit \(OU\)"),
        ("cn", r"Common Name \(CN\)"),
    ]
    for key, pattern in field_order:
        child.expect(pattern)
        value = ssl_fields.get(f"{prefix}_{key}") or ssl_fields.get(key, "")
        child.sendline(value)
        display = value if value else "(default)"
        log.info("  %s -> %s", key, display)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Automated Hexio C2 installer for cloud-init",
    )

    p.add_argument(
        "--license-url",
        default=os.environ.get("HEXIO_LICENSE_URL", ""),
        help="URL to fetch license blob from (or set HEXIO_LICENSE_URL env var)",
    )
    p.add_argument(
        "--license-token",
        default=os.environ.get("HEXIO_LICENSE_TOKEN", ""),
        help="Bearer token for license API (or set HEXIO_LICENSE_TOKEN env var)",
    )
    p.add_argument(
        "--license-blob",
        default=os.environ.get("HEXIO_LICENSE_BLOB", ""),
        help="License blob string directly (or set HEXIO_LICENSE_BLOB env var). "
             "Ignored if --license-url is set.",
    )
    p.add_argument(
        "--root-password",
        default=os.environ.get("HEXIO_ROOT_PASSWORD", ""),
        help="Root user password for the teamserver (or set HEXIO_ROOT_PASSWORD env var)",
    )
    p.add_argument(
        "--cleanup",
        action="store_true",
        default=True,
        help="Clean up compiler scripts and container tarballs after install (default: true)",
    )
    p.add_argument(
        "--no-cleanup",
        action="store_false",
        dest="cleanup",
        help="Keep compiler scripts and container tarballs",
    )
    p.add_argument(
        "--skip-install-sh",
        action="store_true",
        help="Skip running install.sh (already ran separately)",
    )
    p.add_argument(
        "--skip-setup-wizard",
        action="store_true",
        help="Skip the teamserver setup wizard",
    )
    p.add_argument(
        "--start",
        action="store_true",
        help="Start the teamserver in the background after setup completes",
    )
    p.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Suppress pexpect output (hides license blob from logs)",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Timeout in seconds for each phase (default: {DEFAULT_TIMEOUT})",
    )
    p.add_argument(
        "--teamserver-dir",
        default=TEAMSERVER_DIR,
        help=f"Path to teamserver directory (default: {TEAMSERVER_DIR})",
    )

    # Queue / semaphore
    q = p.add_argument_group("Queue (limit concurrent installs)")
    q.add_argument(
        "--queue-url",
        default=os.environ.get("HEXIO_QUEUE_URL", ""),
        help="URL of the queue API (e.g. https://dev.cyberxredteam.org/api/queue). "
             "If not set, queue is skipped.",
    )
    q.add_argument(
        "--queue-token",
        default=os.environ.get("HEXIO_QUEUE_TOKEN", ""),
        help="Bearer token for queue API (defaults to --license-token if not set)",
    )

    # SSL certificate overrides — empty means accept the built-in default
    ssl = p.add_argument_group("SSL certificate fields (empty = accept default)")
    ssl.add_argument("--ssl-country", default="", help="Country (C)")
    ssl.add_argument("--ssl-state", default="", help="State (ST)")
    ssl.add_argument("--ssl-city", default="", help="City (L)")
    ssl.add_argument("--ssl-org", default="", help="Organization (O)")
    ssl.add_argument("--ssl-ou", default="", help="Organizational Unit (OU)")
    ssl.add_argument("--ssl-cn", default="", help="Common Name (CN) for base cert")
    ssl.add_argument("--ssl-ts-cn", default="", help="Common Name (CN) for teamserver cert")

    return p.parse_args()


def main() -> None:
    args = parse_args()

    # Allow overriding the install directory
    global TEAMSERVER_DIR, INSTALL_SCRIPT
    TEAMSERVER_DIR = args.teamserver_dir
    INSTALL_SCRIPT = os.path.join(TEAMSERVER_DIR, "install.sh")

    # --- Resolve license blob ---
    license_blob = args.license_blob
    if not args.skip_setup_wizard:
        if args.license_url:
            if not args.license_token:
                log.error("--license-token is required when using --license-url")
                sys.exit(1)
            license_blob = fetch_license_blob(args.license_url, args.license_token)
        elif not license_blob:
            log.error("Provide --license-url/--license-token or --license-blob (or env vars)")
            sys.exit(1)

        if not args.root_password:
            log.error("--root-password is required (or set HEXIO_ROOT_PASSWORD)")
            sys.exit(1)

    # Build SSL fields dict
    ssl_fields = {
        "country": args.ssl_country,
        "state": args.ssl_state,
        "city": args.ssl_city,
        "org": args.ssl_org,
        "ou": args.ssl_ou,
        # base cert CN
        "base_cn": args.ssl_cn,
        # teamserver cert CN
        "teamserver_cn": args.ssl_ts_cn,
    }

    # --- Queue: acquire slot (if configured) ---
    queue_url = args.queue_url
    queue_token = args.queue_token or args.license_token
    slot_id = None

    if queue_url:
        hostname = os.uname().nodename
        log.info("Waiting for queue slot from %s (hostname=%s) ...", queue_url, hostname)
        slot_id = acquire_queue_slot(queue_url, queue_token, hostname)

    total_t0 = time.time()
    result = "success"

    try:
        # --- Phase 1: install.sh ---
        if not args.skip_install_sh:
            if not os.path.isfile(INSTALL_SCRIPT):
                log.error("install.sh not found at %s", INSTALL_SCRIPT)
                sys.exit(1)
            t0 = time.time()
            run_install_sh(cleanup=args.cleanup, timeout=args.timeout, quiet=args.quiet)
            elapsed = time.time() - t0
            mins, secs = divmod(int(elapsed), 60)
            log.info("install.sh completed in %dm %ds", mins, secs)
        else:
            log.info("Skipping install.sh (--skip-install-sh)")

        # --- Phase 2: setup wizard ---
        if not args.skip_setup_wizard:
            if not os.path.isfile(TEAMSERVER_BIN):
                log.error("hexioteamserver binary not found at %s", TEAMSERVER_BIN)
                sys.exit(1)
            t0 = time.time()
            run_teamserver_setup(
                license_blob=license_blob,
                root_password=args.root_password,
                ssl_fields=ssl_fields,
                timeout=args.timeout,
                quiet=args.quiet,
            )
            elapsed = time.time() - t0
            mins, secs = divmod(int(elapsed), 60)
            log.info("Setup wizard completed in %dm %ds", mins, secs)
        else:
            log.info("Skipping setup wizard (--skip-setup-wizard)")

        # --- Phase 3: start teamserver ---
        if args.start:
            if not os.path.isfile(TEAMSERVER_BIN):
                log.error("hexioteamserver binary not found at %s", TEAMSERVER_BIN)
                sys.exit(1)
            log.info("Starting hexioteamserver in the background ...")
            log_dir = "/var/log/hexio"
            log_file = os.path.join(log_dir, "teamserver.log")
            os.makedirs(log_dir, exist_ok=True)
            proc = subprocess.Popen(
                [TEAMSERVER_BIN],
                cwd=TEAMSERVER_DIR,
                stdout=open(log_file, "a"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            log.info("Teamserver started (PID %d), logging to %s",
                     proc.pid, log_file)

    except Exception:
        result = "error"
        raise
    finally:
        total_elapsed = time.time() - total_t0
        mins, secs = divmod(int(total_elapsed), 60)
        log.info("Hexio automated setup finished — result=%s, total elapsed: %dm %ds",
                 result, mins, secs)

        # --- Queue: release slot ---
        if slot_id and queue_url:
            release_queue_slot(queue_url, queue_token, slot_id,
                               result, int(total_elapsed))


if __name__ == "__main__":
    main()
