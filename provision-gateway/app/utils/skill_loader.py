"""Skill loader — reads SKILL.md and extracts rules for LLM config generation.

The SKILL.md from _users_provision/skills/provision-api defines the authoritative
template conventions used by the provision tool. This module extracts the relevant
sections so the LLM prompts always match the skill definition.
"""

from __future__ import annotations

import re
from pathlib import Path

_SKILL_PATH = Path(__file__).parent.parent / "skills" / "provision-api" / "SKILL.md"

# Cached extracted sections
_compose_rules: str | None = None
_nginx_rules: str | None = None


def _load_skill() -> str:
    """Load the full SKILL.md content."""
    if _SKILL_PATH.exists():
        return _SKILL_PATH.read_text()
    return ""


def _extract_section(content: str, heading: str) -> str:
    """Extract a section from the SKILL.md by heading name.

    Finds a line matching `### {heading}` and returns all content
    until the next heading of the same or higher level.
    """
    pattern = rf"^### {re.escape(heading)}.*$"
    lines = content.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(pattern, line):
            start = i + 1
        elif start is not None and re.match(r"^#{1,3}\s", line):
            # Reached next heading — stop
            return "\n".join(lines[start:i]).strip()
    if start is not None:
        return "\n".join(lines[start:]).strip()
    return ""


def get_compose_rules() -> str:
    """Get the compose file rules from SKILL.md."""
    global _compose_rules
    if _compose_rules is None:
        content = _load_skill()
        if content:
            _compose_rules = _extract_section(content, "Rules for the compose file")
        if not _compose_rules:
            _compose_rules = _fallback_compose_rules()
    return _compose_rules


def get_nginx_rules() -> str:
    """Get the nginx conf rules from SKILL.md (Creating nginx.conf section)."""
    global _nginx_rules
    if _nginx_rules is None:
        content = _load_skill()
        if content:
            # The nginx rules span multiple sub-sections under "Creating nginx.conf"
            # Extract from "Creating nginx.conf when none is provided" through
            # to the next ##-level heading
            lines = content.splitlines()
            start = None
            for i, line in enumerate(lines):
                if line.startswith("## Creating nginx.conf"):
                    start = i
                elif start is not None and line.startswith("## "):
                    _nginx_rules = "\n".join(lines[start:i]).strip()
                    break
            if start is not None and _nginx_rules is None:
                _nginx_rules = "\n".join(lines[start:]).strip()
        if not _nginx_rules:
            _nginx_rules = _fallback_nginx_rules()
    return _nginx_rules


def get_compose_template() -> str:
    """Get the compose file template example from SKILL.md."""
    content = _load_skill()
    # Extract the ```yaml block under "Template for compose file"
    if "Template for compose file" in content:
        idx = content.index("Template for compose file")
        # Find the next ```yaml block after this heading
        fence_start = content.find("```yaml", idx)
        if fence_start >= 0:
            fence_end = content.find("```", fence_start + 7)
            if fence_end >= 0:
                return content[fence_start + 7:fence_end].strip()
    return ""


def get_nginx_template() -> str:
    """Get the nginx conf template example from SKILL.md."""
    content = _load_skill()
    if "Nginx conf template" in content:
        idx = content.index("Nginx conf template")
        fence_start = content.find("```nginx", idx)
        if fence_start >= 0:
            fence_end = content.find("```", fence_start + 8)
            if fence_end >= 0:
                return content[fence_start + 8:fence_end].strip()
    return ""


# ---------------------------------------------------------------------------
# Fallback rules (used when SKILL.md is unavailable)
# ---------------------------------------------------------------------------

def _fallback_compose_rules() -> str:
    return """1. Use `build: .` (or a subdirectory path) so the Dockerfile is the build context.
   Do NOT hardcode `image:` unless the docs specify a pre-built image.

2. Do NOT set `container_name:` directly — the provision tool's auto-converter will
   rewrite container_name to use per-user prefixes. Simply name the service descriptively.

3. Do NOT hardcode host ports — the provision tool strips ports on conversion.
   Use `expose:` for internal ports instead.

4. Use named volumes for persistent data so the converter can map them to per-user paths.

5. Use ${VAR} syntax for runtime secrets (API keys, DB passwords).

6. Define a network so per-user containers are isolated.

7. Provide a healthcheck if possible."""


def _fallback_nginx_rules() -> str:
    return """1. server_name will be templated to {{ hostname }}.

2. proxy_pass to http://SERVICE_NAME:PORT — the host MUST match a compose service key exactly.

3. Include auth_basic and auth_basic_user_file directives.

4. Include proxy headers (Host, X-Real-IP, X-Forwarded-For, X-Forwarded-Proto).

5. WebSocket support (Upgrade, Connection headers).

6. client_max_body_size 100m."""
