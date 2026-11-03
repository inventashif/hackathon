#!/usr/bin/env python3
"""MechTech AI Multi-Interface Server Runner.

Supports:
1. Plain HTTP on port 8000 (accessible on LAN with no SSL/certificate warnings)
2. HTTPS on port 8443 (self-signed SSL with microphone support)
3. Cloudflare Quick Tunnel (public HTTPS with valid trusted certificate, works on any phone/Wi-Fi/cellular)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

import uvicorn
from backend.main import app

ROOT = Path(__file__).resolve().parent.parent
CERT_DIR = ROOT / "data" / "devcert"
CERT_FILE = CERT_DIR / "mechtech-ai.crt"
KEY_FILE = CERT_DIR / "mechtech-ai.key"


def get_lan_ips() -> list[str]:
    """Detect all active local IPv4 addresses."""
    ips: list[str] = []
    try:
        out = subprocess.check_output(["hostname", "-I"], text=True, stderr=subprocess.DEVNULL)
        for part in out.strip().split():
            if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", part) and not part.startswith("127."):
                ips.append(part)
    except Exception:
        pass

    if not ips:
        try:
            out = subprocess.check_output(
                ["ip", "-4", "addr", "show", "scope", "global"], text=True, stderr=subprocess.DEVNULL
            )
            for match in re.finditer(r"inet\s+(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/\d+", out):
                ip = match.group(1)
                if not ip.startswith("127.") and ip not in ips:
                    ips.append(ip)
        except Exception:
            pass

    return ips


def find_cloudflared() -> Optional[str]:
    """Locate cloudflared binary on the system."""
    which = shutil.which("cloudflared")
    if which:
        return which
    candidates = [
        Path.home() / ".local" / "bin" / "cloudflared",
        Path("/usr/local/bin/cloudflared"),
        Path("/usr/bin/cloudflared"),
        Path("/snap/bin/cloudflared"),
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


async def start_cloudflared_tunnel(cloudflared_path: str, local_port: int) -> tuple[subprocess.Popen, Optional[str]]:
    """Start cloudflared quick tunnel and capture the public trycloudflare.com URL."""
    cmd = [
        cloudflared_path,
        "tunnel",
        "--url",
        f"http://127.0.0.1:{local_port}",
        "--no-autoupdate",
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    tunnel_url: Optional[str] = None
    loop = asyncio.get_running_loop()

    def read_tunnel_url() -> Optional[str]:
        nonlocal tunnel_url
        if not proc.stdout:
            return None
        start = time.time()
        for line in iter(proc.stdout.readline, ""):
            match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
            if match:
                tunnel_url = match.group(0)
                return tunnel_url
            if proc.poll() is not None or (time.time() - start > 15):
                break
        return None

    try:
        tunnel_url = await asyncio.wait_for(loop.run_in_executor(None, read_tunnel_url), timeout=12.0)
    except asyncio.TimeoutError:
        pass

    return proc, tunnel_url


def ensure_certificates(lan_ips: list[str]) -> bool:
    """Ensure SSL certificates exist with current LAN IPs in SAN."""
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    needs_renewal = not (CERT_FILE.is_file() and KEY_FILE.is_file() and CERT_FILE.stat().st_size > 0)

    if not needs_renewal and lan_ips:
        try:
            out = subprocess.check_output(
                ["openssl", "x509", "-in", str(CERT_FILE), "-noout", "-ext", "subjectAltName"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            for ip in lan_ips:
                if f"IP Address:{ip}" not in out:
                    needs_renewal = True
                    break
        except Exception:
            needs_renewal = True

    if needs_renewal and shutil.which("openssl"):
        san_entries = ["DNS:localhost", "IP:127.0.0.1"]
        for ip in lan_ips:
            san_entries.append(f"IP:{ip}")
        san_str = ",".join(san_entries)

        try:
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-sha256",
                    "-nodes",
                    "-days",
                    "825",
                    "-keyout",
                    str(KEY_FILE),
                    "-out",
                    str(CERT_FILE),
                    "-subj",
                    "/CN=MechTech AI",
                    "-addext",
                    f"subjectAltName={san_str}",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            print(f"[Warning] Could not generate self-signed SSL certificate: {exc}", file=sys.stderr)
            return False

    return CERT_FILE.is_file() and KEY_FILE.is_file() and CERT_FILE.stat().st_size > 0


async def main_async() -> None:
    parser = argparse.ArgumentParser(description="MechTech AI Multi-Interface Server")
    parser.add_argument("--port", type=int, default=8000, help="Plain HTTP port (default: 8000)")
    parser.add_argument("--https-port", type=int, default=8443, help="HTTPS port (default: 8443)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--no-tunnel", action="store_true", help="Disable Cloudflare Quick Tunnel")
    parser.add_argument("--tunnel", action="store_true", help="Enable Cloudflare Quick Tunnel")
    parser.add_argument("--no-https", action="store_true", help="Disable local HTTPS server")
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn auto-reload")
    args = parser.parse_args()

    lan_ips = get_lan_ips()
    primary_lan = lan_ips[0] if lan_ips else "127.0.0.1"
    has_certs = False if args.no_https else ensure_certificates(lan_ips)

    tunnel_proc: Optional[subprocess.Popen] = None
    tunnel_url: Optional[str] = None

    cloudflared = find_cloudflared()
    use_tunnel = not args.no_tunnel if cloudflared else False
    if args.tunnel:
        use_tunnel = True

    if use_tunnel and cloudflared:
        print("\n[Tunnel] Establishing Cloudflare Quick Tunnel...", flush=True)
        tunnel_proc, tunnel_url = await start_cloudflared_tunnel(cloudflared, args.port)

    # Print comprehensive access banner
    print("\n" + "=" * 68)
    print("      MECHTECH AI / NEXUSSOP - INDUSTRIAL COPILOT IS READY")
    print("=" * 68)

    if tunnel_url:
        print("\n  ★ 1. GLOBAL / PHONE ACCESS (Recommended):")
        print(f"     URL:  {tunnel_url}")
        print("     • Works on any phone, tablet, cellular data or remote Wi-Fi.")
        print("     • Trusted SSL certificate (no security warnings).")
        print("     • Full microphone and live voice chat support.")

    print("\n  ★ 2. LOCAL NETWORK (Plain HTTP - Instant & Reliable):")
    for ip in lan_ips:
        print(f"     http://{ip}:{args.port}")
    print(f"     http://localhost:{args.port} (on this computer)")
    print("     • Immediate access with ZERO certificate warnings or empty replies.")

    if has_certs:
        print(f"\n  ★ 3. LOCAL NETWORK (HTTPS with Self-Signed Certificate):")
        for ip in lan_ips:
            print(f"     https://{ip}:{args.https_port}")
        print(f"     https://localhost:{args.https_port} (on this computer)")
        print("     • Enables microphone on LAN (accept the browser certificate warning).")

    print("\n" + "=" * 68 + "\n", flush=True)

    # Configure servers
    config_http = uvicorn.Config(
        "backend.main:app" if args.reload else app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    server_http = uvicorn.Server(config_http)

    tasks = [server_http.serve()]

    server_https: Optional[uvicorn.Server] = None
    if has_certs:
        config_https = uvicorn.Config(
            "backend.main:app" if args.reload else app,
            host=args.host,
            port=args.https_port,
            reload=args.reload,
            ssl_certfile=str(CERT_FILE),
            ssl_keyfile=str(KEY_FILE),
            log_level="warning",
        )
        server_https = uvicorn.Server(config_https)
        tasks.append(server_https.serve())

    loop = asyncio.get_running_loop()

    def shutdown():
        server_http.should_exit = True
        if server_https:
            server_https.should_exit = True
        if tunnel_proc and tunnel_proc.poll() is None:
            tunnel_proc.terminate()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown)
        except NotImplementedError:
            pass

    try:
        await asyncio.gather(*tasks)
    finally:
        if tunnel_proc and tunnel_proc.poll() is None:
            tunnel_proc.terminate()


def main():
    try:
        asyncio.run(main_async())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
