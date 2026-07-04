"""Curl service — runs curl from within the gateway container to test URLs."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field


@dataclass
class CurlResult:
    url: str
    http_code: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    body_preview: str = ""
    time_total_ms: float = 0.0
    error: str | None = None


async def test_url(
    url: str,
    include_auth: bool = False,
    username: str = "",
    password: str = "",
    follow_redirect: bool = True,
    timeout_sec: int = 10,
) -> CurlResult:
    """Run curl to test a URL and return structured results."""
    cmd = [
        "curl", "-s", "-o", "-",
        "-w", "\n%{http_code}|%{time_total}|%{size_download}",
        "-D", "-",
        "--max-time", str(timeout_sec),
    ]
    if follow_redirect:
        cmd.append("-L")
    if include_auth and username:
        cmd.extend(["-u", f"{username}:{password}"])
    cmd.append(url)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_sec + 5
        )
        output = stdout.decode("utf-8", errors="replace")

        # Parse output: headers + body + status line
        # The status line is the last line with format: http_code|time_total|size_download
        parts = output.rsplit("\n", 1)
        if len(parts) == 2:
            headers_body = parts[0]
            status_line = parts[1]
        else:
            headers_body = output
            status_line = "0|0|0"

        status_parts = status_line.split("|")
        http_code = int(status_parts[0]) if status_parts[0].isdigit() else 0
        time_total = float(status_parts[1]) if len(status_parts) > 1 else 0.0

        # Separate headers from body
        header_end = headers_body.find("\r\n\r\n")
        if header_end == -1:
            header_end = headers_body.find("\n\n")

        if header_end > 0:
            header_text = headers_body[:header_end]
            body = headers_body[header_end:].strip()
        else:
            header_text = ""
            body = headers_body

        # Parse headers
        headers = {}
        for line in header_text.split("\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()

        return CurlResult(
            url=url,
            http_code=http_code,
            headers=headers,
            body_preview=body[:2000],
            time_total_ms=round(time_total * 1000, 1),
        )

    except asyncio.TimeoutError:
        return CurlResult(url=url, error="Request timed out")
    except Exception as e:
        return CurlResult(url=url, error=str(e))
