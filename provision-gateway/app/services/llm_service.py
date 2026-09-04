"""LLM service — OpenAI-compatible client for config generation.

Implements the design's *agent with tools* generation: a minimal in-house
bounded tool loop (web_fetch / read_file / list_files — a closed allowlist,
NO bash), mechanical validation self-repair, and a single-shot fallback with
gateway-side URL prefetch when the BYOK model rejects tool calls.

Generation context (decision 5a): raw contents of the selected base files
only + prompt + deploy metadata (user_name / label / domain / hostname
convention ``{service}-{user}-{label}.{domain}``) + activated profiles. When
compose itself is missing, the small closed set of app manifests (Dockerfile*,
requirements.txt, pyproject.toml, package.json, go.mod, ...) is added via the
RepoContext shallow scan.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from ..models.llm_config import LLMConfig
from ..utils.crypto import decrypt_api_key, encrypt_api_key
from ..utils.file_scanner import RepoContext
from .config_validation import validate_compose, validate_env

# ---------------------------------------------------------------------------
# Agent bounds (design §Generation — Bounds & degradation)
# ---------------------------------------------------------------------------

TOOL_ROUNDS_MAX = 6          # max agent loop iterations (4–6)
WEB_FETCH_MAX_BYTES = 64 * 1024      # per-page size cap
TOTAL_FETCH_MAX_BYTES = 512 * 1024   # total fetched bytes cap
FETCH_TIMEOUT = 30.0
MAX_BASE_FILE_BYTES = 128 * 1024

# App-manifest carve-out: the small closed set of build/dependency/runtime
# convention files included when compose itself is missing (§Generation 5a).
MANIFEST_FILENAMES = (
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg",
    "package.json", "go.mod", "Cargo.toml", "pom.xml", "build.gradle",
    "Gemfile", "composer.json", "Makefile", "Procfile",
    "main.py", "app.py", "manage.py", "wsgi.py", "asgi.py",
    "alembic.ini", "vite.config.js", "vite.config.ts", "next.config.js",
    "next.config.mjs",
)

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "Fetch a documentation page over http/https (the project's official "
                "deployment docs). Only the first 64KB is returned."
            ),
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "http(s) URL"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a text file from the service project (confined to the recipe "
                "directory / project root). Use for Dockerfiles, configs, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "project-root-relative file path"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List the files of the recipe directory (and project root when the "
                "recipe is a subdirectory). Shallow listing."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]


class LLMService:
    """Manages LLM configuration and generates service config files."""

    # ------------------------------------------------------------------
    # Config management (multi-config)
    # ------------------------------------------------------------------

    def list_configs(self, db: Session) -> list[dict]:
        """List all LLM configs."""
        configs = db.query(LLMConfig).order_by(LLMConfig.id).all()
        return [c.to_dict() for c in configs]

    def get_config(self, db: Session) -> dict:
        """Get the currently active LLM config (backward compat)."""
        config = db.query(LLMConfig).filter(LLMConfig.is_active == True).first()
        if not config:
            return {"mode": "byok", "agent_url": None, "agent_model": None,
                    "byok_configured": False, "byok_model": None,
                    "byok_api_key_masked": None, "is_active": False, "system_prompt": None}
        return config.to_dict()

    def create_config(self, db: Session, data: dict) -> LLMConfig:
        """Create a new LLM config.

        Local agent / provision agent are FUTURE features (tasks-21072026
        #3.1). This API only supports BYOK: any ``mode='local_agent'`` and
        the ``agent_url`` / ``agent_model`` fields are ignored and normalized
        to BYOK so the backend never drives generation from a local-agent
        config (GAP-2).
        """
        mode = data.get("mode", "byok")
        if mode != "byok":
            mode = "byok"
        config = LLMConfig(
            mode=mode,
            byok_base_url=data.get("byok_base_url", ""),
            byok_model=data.get("byok_model", ""),
            system_prompt=data.get("system_prompt", ""),
            is_active=False,
        )
        if data.get("byok_api_key"):
            config.byok_api_key_enc = encrypt_api_key(data["byok_api_key"])
        db.add(config)
        db.commit()
        db.refresh(config)
        return config

    def activate_config(self, db: Session, config_id: int) -> LLMConfig | None:
        """Activate a config, deactivating all others."""
        config = db.query(LLMConfig).filter(LLMConfig.id == config_id).first()
        if not config:
            return None
        db.query(LLMConfig).filter(LLMConfig.is_active == True).update({"is_active": False})
        config.is_active = True
        db.commit()
        db.refresh(config)
        return config

    def delete_config(self, db: Session, config_id: int) -> bool:
        """Delete an LLM config."""
        config = db.query(LLMConfig).filter(LLMConfig.id == config_id).first()
        if not config:
            return False
        db.delete(config)
        db.commit()
        return True

    def save_config(self, db: Session, data: dict) -> LLMConfig:
        """Save/update config (backward compat — creates or updates active).

        Local agent / provision agent are FUTURE features (tasks-21072026
        #3.1). Any ``mode='local_agent'`` is normalized to BYOK and the
        ``agent_url`` / ``agent_model`` fields are never persisted (GAP-2).
        """
        config = db.query(LLMConfig).filter(LLMConfig.is_active == True).first()
        if not config:
            config = LLMConfig()
            db.add(config)
        mode = data.get("mode")
        if mode is None:
            mode = config.mode or "byok"
        if mode != "byok":
            mode = "byok"
        config.mode = mode
        # Clear local-agent fields — never drive generation from a local-agent config.
        config.agent_url = None
        config.agent_model = None
        config.byok_base_url = data.get("byok_base_url", config.byok_base_url)
        config.byok_model = data.get("byok_model", config.byok_model)
        config.system_prompt = data.get("system_prompt", config.system_prompt)
        config.is_active = True
        if data.get("byok_api_key"):
            config.byok_api_key_enc = encrypt_api_key(data["byok_api_key"])
        db.commit()
        db.refresh(config)
        return config

    # ------------------------------------------------------------------
    # LLM calls
    # ------------------------------------------------------------------

    def _resolve_endpoint(self, db: Session) -> tuple[str, str, dict]:
        """Resolve which LLM endpoint to use.

        Returns (base_url, model, extra_headers).

        Local agent / provision agent are FUTURE features (tasks-21072026
        #3.1). The backend must NOT drive generation from a local-agent
        config: only an active BYOK config is used; otherwise a neutral
        OpenAI-compatible default is returned (GAP-2).
        """
        config = db.query(LLMConfig).filter(LLMConfig.is_active == True).first()

        if config and config.mode == "byok" and config.byok_api_key_enc:
            api_key = decrypt_api_key(config.byok_api_key_enc)
            base = config.byok_base_url or "https://api.openai.com/v1"
            model = config.byok_model or "gpt-4o"
            headers = {"Authorization": f"Bearer {api_key}"}
        else:
            base = "http://localhost:11434/v1"
            model = "llama3.1:8b"
            headers = {}

        return base, model, headers

    async def test_connection(self, db: Session) -> dict:
        """Test the LLM connection with a simple chat request."""
        base_url, model, headers = self._resolve_endpoint(db)

        messages = [
            {"role": "user", "content": "Hello! Respond with just 'OK' if you can read this."}
        ]

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": 10,
                },
                headers=headers,
            )

            if resp.status_code != 200:
                return {
                    "success": False,
                    "error": f"HTTP {resp.status_code}: {resp.text[:500]}",
                    "model": model,
                }

            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

            return {
                "success": True,
                "latency_ms": 0,  # httpx doesn't expose timing easily
                "model": model,
                "response_preview": content[:200],
            }

    # ------------------------------------------------------------------
    # Tool execution (closed allowlist)
    # ------------------------------------------------------------------

    async def _exec_tool(self, name: str, arguments: dict, context: dict) -> str:
        """Execute one allowlisted tool; returns a text result for the model."""
        if name == "web_fetch":
            return await self._tool_web_fetch(arguments)
        if name == "read_file":
            return self._tool_read_file(arguments, context)
        if name == "list_files":
            return self._tool_list_files(context)
        return f"ERROR: unknown tool: {name}"

    async def _tool_web_fetch(self, arguments: dict) -> str:
        url = str(arguments.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return "ERROR: web_fetch only supports http/https URLs"
        try:
            async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
                resp = await client.get(url)
                if resp.status_code >= 400:
                    return f"ERROR: HTTP {resp.status_code} fetching {url}"
                body = resp.text
                if len(body.encode("utf-8", errors="replace")) > WEB_FETCH_MAX_BYTES:
                    # Truncate on a UTF-8-safe boundary.
                    body = body[:WEB_FETCH_MAX_BYTES]
                # Strip tags for readability (docs pages are HTML).
                text = re.sub(r"<script[^>]*>.*?</script>", " ", body, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"\s+", " ", text).strip()
                return f"<fetched {url} ({len(text)} chars)>\n{text[:WEB_FETCH_MAX_BYTES]}"
        except Exception as exc:
            return f"ERROR: web_fetch failed: {exc}"

    def _resolve_project_paths(self, context: dict) -> tuple[Path, Path]:
        """Resolve (project_dir, recipe_dir) from context (path-confined)."""
        project_dir = Path(context.get("project_dir") or settings.SOURCE_PROJECTS_DIR)
        recipe_path = context.get("recipe_path") or ""
        recipe_dir = project_dir
        if recipe_path and recipe_path not in (".", ""):
            recipe_dir = project_dir / recipe_path
        return project_dir.resolve(), recipe_dir.resolve()

    def _tool_read_file(self, arguments: dict, context: dict) -> str:
        raw = str(arguments.get("path") or "").strip().lstrip("/")
        project_dir, recipe_dir = self._resolve_project_paths(context)
        if not raw or ".." in raw.split("/") or "\\" in raw:
            return f"ERROR: invalid path: {raw!r}"
        candidate = (project_dir / raw).resolve()
        if not str(candidate).startswith(str(project_dir) + "/"):
            return "ERROR: path outside project root"
        # Confined to the recipe dir + (recipe ≠ root) the project root.
        allowed = str(recipe_dir) + "/"
        if not (str(candidate).startswith(allowed) or str(candidate).startswith(str(project_dir) + "/")):
            return "ERROR: path outside recipe/project scope"
        if not candidate.is_file():
            return f"ERROR: file not found: {raw}"
        try:
            content = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"ERROR: cannot read {raw}: {exc}"
        if len(content) > WEB_FETCH_MAX_BYTES:
            content = content[:WEB_FETCH_MAX_BYTES]
        return f"<file {raw}>\n{content}"

    def _tool_list_files(self, context: dict) -> str:
        project_dir, recipe_dir = self._resolve_project_paths(context)
        lines: list[str] = []
        for root in ([recipe_dir] if recipe_dir == project_dir else [recipe_dir, project_dir]):
            if not root.is_dir():
                continue
            try:
                names = sorted(e.name for e in root.iterdir())
            except OSError:
                continue
            label = "recipe" if root == recipe_dir else "project root"
            lines.append(f"--- {label}: {root.name} ---")
            lines.extend(names)
        return "\n".join(lines) if lines else "(empty recipe — no files found)"

    # ------------------------------------------------------------------
    # Prompt construction (context 5a)
    # ------------------------------------------------------------------

    def _build_generation_prompt(self, config_type: str, context: dict) -> str:
        """Build the user prompt for one generation phase.

        Context 5a: raw contents of the selected base files ONLY + prompt +
        deploy metadata + activated profiles; the app-manifest carve-out when
        compose itself is missing.
        """
        from ..utils.skill_loader import get_compose_rules, get_nginx_rules, get_compose_template, get_nginx_template

        desc = context.get("repo_description", "an application")
        user_prompt = (context.get("prompt") or "").strip()
        deploy_meta = context.get("deploy_metadata") or {}
        profiles = context.get("profiles") or []
        compose_service_names = context.get("compose_service_names") or []

        parts: list[str] = []
        parts.append(f"Generate the {config_type} for {desc}.")

        if user_prompt:
            parts.append(f"\nOperator prompt (follow it exactly; doc URLs inside may be fetched with the web_fetch tool):\n{user_prompt}")

        # Base files (decision 5a) — raw contents only.
        base_files = context.get("base_files") or {}
        if base_files:
            blocks = []
            for fname, content in base_files.items():
                blocks.append(f"--- base file: {fname} ---\n{content}")
            parts.append("\nSelected base files (their content is authoritative for service names, volumes and variables):\n" + "\n\n".join(blocks))

        # App-manifest carve-out (compose missing).
        manifests = context.get("manifests") or {}
        if manifests:
            blocks = []
            for fname, content in manifests.items():
                blocks.append(f"--- {fname} ---\n{content}")
            parts.append("\nApp manifests (compose is missing; these describe the app):\n" + "\n\n".join(blocks))

        # Deploy metadata + profiles.
        meta_lines = []
        if deploy_meta.get("user_name"):
            meta_lines.append(f"user_name: {deploy_meta['user_name']}")
        if deploy_meta.get("label") is not None:
            meta_lines.append(f"label: {deploy_meta['label']}")
        if deploy_meta.get("domain"):
            meta_lines.append(f"domain: {deploy_meta['domain']}")
        if deploy_meta.get("service_name"):
            hostname = f"{deploy_meta['service_name']}-{deploy_meta.get('user_name','x')}-{deploy_meta.get('label','0')}.{deploy_meta.get('domain','localhost')}"
            meta_lines.append(f"hostname convention: {hostname}")
        if profiles:
            meta_lines.append(f"activated compose profiles: {', '.join(profiles)}")
        if compose_service_names:
            meta_lines.append(f"compose service names: {', '.join(compose_service_names)}")
        if meta_lines:
            parts.append("\nDeploy metadata:\n" + "\n".join(meta_lines))

        if config_type == "docker_compose":
            rules = get_compose_rules()
            template = get_compose_template()
            template_block = f"\nReference template (fill in placeholders):\n```yaml\n{template}\n```" if template else ""
            parts.append(f"""
The generated file is used by the provision tool (provision-api) which converts
it to a per-user Jinja2 template. Follow these rules EXACTLY:

{rules}

MINIMAL-COMPOSE HARD RULE: one service unless the app manifests clearly
justify more (e.g. a database/cache service); no host port publishing (the
converter strips ports — use `expose:` for internal ports); no padding.
If profiles are listed above, gate optional services behind those exact
profile names (e.g. a database behind the "postgresql" profile).
{template_block}
Output ONLY the raw YAML, no markdown fences, no explanations.""")

        elif config_type == "nginx_conf":
            rules = get_nginx_rules()
            template = get_nginx_template()
            template_block = f"\nReference template (fill in placeholders):\n```nginx\n{template}\n```" if template else ""
            hint = ""
            if compose_service_names:
                hint = (
                    "\nCRITICAL: the compose defines these service(s): "
                    + ", ".join(compose_service_names)
                    + ".\nYour proxy_pass MUST use one of these exact service names as the host "
                    "(e.g. proxy_pass http://<service>:PORT;)."
                )
            parts.append(f"""
Generate an nginx reverse proxy configuration. It sits behind provision-nginx,
serving hostname {deploy_meta.get('domain','localhost')} with basic auth support.{hint}
Follow these rules from the provision-api skill:

{rules}
{template_block}
Output ONLY the raw nginx config, no markdown fences.""")

        elif config_type == "env_file":
            meta_bits = f" for user '{deploy_meta.get('user_name','?')}' label {deploy_meta.get('label','0')} on {deploy_meta.get('domain','localhost')}"
            parts.append(f"""
Generate a .env file (interpolation env for docker compose ${'{VAR}'} substitution){meta_bits}.
The compose file's ${'{VAR}'} references determine the required keys — every
no-default variable MUST be present in the output.
Generate a fresh random SECRET_KEY for this instance (unique per deployment).
{('Activated profiles: ' + ', '.join(profiles) + ' — the env must match the services those profiles gate (e.g. a postgresql profile needs a working DATABASE_URL).') if profiles else ''}
Output ONLY the raw env file contents, KEY=VALUE per line.""")

        else:
            parts.append(f"\nGenerate the {config_type} file for {desc}.")

        return "\n".join(parts)

    def _build_repair_prompt(self, validation_errors: list[str]) -> str:
        return (
            "The previous attempt FAILED mechanical validation with these errors:\n"
            + "\n".join(f"- {e}" for e in validation_errors)
            + "\n\nRevise the output to fix every error. Output ONLY the corrected raw file content, no markdown fences."
        )

    # ------------------------------------------------------------------
    # LLM round-trip (tool-capable)
    # ------------------------------------------------------------------

    async def _chat(
        self,
        base_url: str,
        model: str,
        headers: dict,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 4000,
    ) -> dict:
        """One chat/completions call; returns the raw response dict (or {})."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }
        if tools:
            payload["tools"] = tools
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
            if resp.status_code != 200:
                return {"_error": f"LLM HTTP {resp.status_code}: {resp.text[:300]}"}
            return resp.json()

    # ------------------------------------------------------------------
    # Single-shot fallback (gateway URL prefetch)
    # ------------------------------------------------------------------

    async def _prefetch_prompt_urls(self, prompt: str) -> str:
        """Fetch any http(s) URLs in the prompt and inline their content.

        Used by the single-shot fallback so docs URLs are honored even when
        the BYOK model rejects tool calls.
        """
        urls = re.findall(r"https?://[^\s)\]\"']+", prompt)
        if not urls:
            return prompt
        injected = []
        seen: set[str] = set()
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            result = await self._tool_web_fetch({"url": url})
            injected.append(result)
        if not injected:
            return prompt
        return prompt + "\n\n--- Pre-fetched documentation (from prompt URLs) ---\n" + "\n\n".join(injected)

    # ------------------------------------------------------------------
    # Public generation API
    # ------------------------------------------------------------------

    async def generate_with_agent(
        self, db: Session, config_type: str, context: dict, progress=None
    ) -> dict:
        """Agentic generation with the closed tool allowlist + self-repair.

        ``context`` keys (all optional): repo_description, prompt, base_files
        {name: content}, manifests {name: content}, deploy_metadata,
        profiles, compose_service_names, compose_paths (for validation),
        project_dir, recipe_path, empty_recipe.

        Returns ``{"generated_content", "filename_suggestion", "warnings",
        "validation": {...}, "tool_rounds": int}``.
        """
        from ..utils.skill_loader import get_compose_rules, get_nginx_rules

        base_url, model, headers = self._resolve_endpoint(db)
        config = db.query(LLMConfig).filter(LLMConfig.is_active == True).first()
        system_prompt = (config.system_prompt if config and config.system_prompt else "") or ""
        rules_bits = []
        if config_type == "docker_compose":
            rules_bits.append(get_compose_rules())
        elif config_type == "nginx_conf":
            rules_bits.append(get_nginx_rules())
        if rules_bits:
            system_prompt += "\n\nProvision rules:\n" + "\n".join(rules_bits)

        user_prompt = self._build_generation_prompt(config_type, context)
        if context.get("empty_recipe") and not (context.get("prompt") or "").strip():
            return {
                "generated_content": "",
                "filename_suggestion": "",
                "warnings": ["Prompt is REQUIRED when no base files exist (empty recipe)."],
                "validation": {"valid": False, "errors": ["empty recipe requires a prompt"]},
                "tool_rounds": 0,
            }

        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        filename = {
            "docker_compose": "docker-compose.yml",
            "nginx_conf": "nginx.conf",
            "env_file": ".env",
            "dockerfile": "Dockerfile",
        }.get(config_type, "generated.txt")

        tool_rounds = 0
        warnings: list[str] = []
        tool_calls_seen = 0
        content = ""
        used_tools_ok = True

        if progress:
            await progress(f"agent round 1/{TOOL_ROUNDS_MAX}")

        while tool_rounds < TOOL_ROUNDS_MAX:
            tool_rounds += 1
            try:
                resp = await self._chat(base_url, model, headers, messages, tools=TOOL_DEFINITIONS)
            except Exception as exc:
                warnings.append(f"LLM call failed: {exc}")
                break
            if "_error" in resp:
                warnings.append(resp["_error"])
                break
            choice = (resp.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            content = msg.get("content") or ""
            calls = msg.get("tool_calls") or []

            if not calls:
                if tool_calls_seen == 0:
                    used_tools_ok = False  # model answered without any tool call
                break

            tool_calls_seen += 1
            # Execute every tool call in this round (bounded by caps).
            messages.append({"role": "assistant", "content": content or None, "tool_calls": calls})
            for call in calls:
                fn = (call.get("function") or {})
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (ValueError, TypeError):
                    args = {}
                result = await self._exec_tool(name, args, context)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id") or "",
                    "content": result,
                })
            if progress:
                await progress(f"agent round {tool_rounds}/{TOOL_ROUNDS_MAX} (tool: {name})")
        else:
            warnings.append(f"tool loop exhausted after {TOOL_ROUNDS_MAX} rounds")

        if not content and (tool_calls_seen == 0 or used_tools_ok is False):
            # Fallback: model rejected/ignored tools → single-shot with
            # gateway-side URL prefetch injected into the prompt.
            warnings.append("tool-call path failed — falling back to single-shot generation with URL prefetch")
            prefetched = await self._prefetch_prompt_urls(user_prompt)
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prefetched})
            try:
                resp = await self._chat(base_url, model, headers, messages)
            except Exception as exc:
                warnings.append(f"single-shot fallback failed: {exc}")
                content = ""
            else:
                if "_error" in resp:
                    warnings.append(resp["_error"])
                    content = ""
                else:
                    content = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            if not content:
                return {
                    "generated_content": "",
                    "filename_suggestion": filename,
                    "warnings": warnings + ["LLM returned no content"],
                    "validation": {"valid": False, "errors": ["LLM returned no content"]},
                    "tool_rounds": tool_rounds,
                }

        extracted = self._extract_code_block(content, config_type)

        # ---- Mechanical validation + self-repair (bounded by the same cap) ----
        validation = await self._validate_generated(config_type, extracted, context)
        while not validation.get("valid") and tool_rounds < TOOL_ROUNDS_MAX and validation.get("repairable", True):
            tool_rounds += 1
            messages.append({"role": "user", "content": self._build_repair_prompt(validation.get("errors", []))})
            if progress:
                await progress(f"self-repair round {tool_rounds}/{TOOL_ROUNDS_MAX}")
            try:
                resp = await self._chat(base_url, model, headers, messages)
            except Exception as exc:
                warnings.append(f"self-repair call failed: {exc}")
                break
            if "_error" in resp:
                warnings.append(resp["_error"])
                break
            extracted = self._extract_code_block(
                ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or "",
                config_type,
            )
            validation = await self._validate_generated(config_type, extracted, context)

        return {
            "generated_content": extracted,
            "filename_suggestion": filename,
            "warnings": warnings,
            "validation": validation,
            "tool_rounds": tool_rounds,
        }

    async def _validate_generated(
        self, config_type: str, content: str, context: dict
    ) -> dict:
        """Mechanical validation of one generated phase (design §Generation)."""
        compose_paths = context.get("compose_paths") or []
        if config_type == "docker_compose":
            if not content.strip():
                return {"valid": False, "errors": ["empty compose output"], "repairable": True}
            # Write draft into the recipe dir so the converter paths stay local.
            project_dir = Path(context.get("project_dir") or settings.SOURCE_PROJECTS_DIR)
            recipe_path = context.get("recipe_path") or ""
            from ..services.file_sets import FileSetError as _FileSetError
            from ..services.file_sets import _recipe_dir as _fs_recipe_dir
            try:
                recipe_dir = _fs_recipe_dir(project_dir, recipe_path)
            except _FileSetError as e:
                return {"valid": False, "errors": [f"invalid recipe_path: {e}"], "repairable": False}
            draft = recipe_dir / ".draft-generated-compose.yml"
            try:
                recipe_dir.mkdir(parents=True, exist_ok=True)
                draft.write_text(content, encoding="utf-8")
                result = validate_compose([draft])
            finally:
                try:
                    draft.unlink()
                except OSError:
                    pass
            result["repairable"] = True
            return result
        if config_type == "nginx_conf":
            from .config_validation import validate_nginx
            if not content.strip():
                return {"valid": False, "errors": ["empty nginx output"], "repairable": True}
            project_dir = Path(context.get("project_dir") or settings.SOURCE_PROJECTS_DIR)
            recipe_path = context.get("recipe_path") or ""
            from ..services.file_sets import FileSetError as _FileSetError
            from ..services.file_sets import _recipe_dir as _fs_recipe_dir
            try:
                recipe_dir = _fs_recipe_dir(project_dir, recipe_path)
            except _FileSetError as e:
                return {"valid": False, "errors": [f"invalid recipe_path: {e}"], "repairable": False}
            draft = recipe_dir / ".draft-generated-nginx.conf"
            try:
                recipe_dir.mkdir(parents=True, exist_ok=True)
                draft.write_text(content, encoding="utf-8")
                result = await validate_nginx(
                    draft, context.get("compose_service_names") or []
                )
            finally:
                try:
                    draft.unlink()
                except OSError:
                    pass
            result["repairable"] = True
            return result
        if config_type == "env_file":
            result = validate_env(content, compose_paths)
            result["repairable"] = True
            return result
        return {"valid": True, "errors": [], "repairable": False}

    # ------------------------------------------------------------------
    # Backward-compat single-shot generation (used by legacy paths)
    # ------------------------------------------------------------------

    async def generate_config(
        self, db: Session, config_type: str, context: dict
    ) -> dict:
        """Generate a config file using LLM (single-shot, legacy path).

        The design's check-deploy auto-generation was retired; this remains
        as a thin single-shot wrapper for any non-agent callers.
        """
        base_url, model, headers = self._resolve_endpoint(db)

        config = db.query(LLMConfig).filter(LLMConfig.is_active == True).first()
        system_prompt = config.system_prompt if config else None

        prompt = self._build_prompt(config_type, context)

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": 4000,
                    "temperature": 0.3,
                },
                headers=headers,
            )

            if resp.status_code != 200:
                return {
                    "generated_content": "",
                    "filename_suggestion": "",
                    "warnings": [f"LLM error: HTTP {resp.status_code}"],
                }

            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

            extracted = self._extract_code_block(content, config_type)

            filename = {
                "docker_compose": "docker-compose.yml",
                "nginx_conf": "nginx.conf",
                "env_file": ".env",
                "dockerfile": "Dockerfile",
            }.get(config_type, "generated.txt")

            return {
                "generated_content": extracted,
                "filename_suggestion": filename,
                "warnings": [],
            }

    # ------------------------------------------------------------------
    # Deploy-time per-user env generation (GAP-19)
    # ------------------------------------------------------------------

    async def generate_per_user_env(
        self, db: Session, context: dict
    ) -> dict:
        """Generate a per-user .env at deployment time.

        Knows user/label/domain (fresh SECRET_KEY per instance). Does NOT
        touch the stored default file set — it writes the per-user file only.
        """
        deploy_meta = context.get("deploy_metadata") or {}
        user_name = deploy_meta.get("user_name", "user")
        label = deploy_meta.get("label", "0")
        domain = deploy_meta.get("domain", "localhost")
        service_name = deploy_meta.get("service_name", "service")

        ctx = dict(context)
        ctx["prompt"] = (
            "This .env is for ONE deployed instance and must contain a fresh, "
            "random SECRET_KEY unique to this instance (and unique DB passwords "
            "if applicable)."
        )
        result = await self.generate_with_agent(db, "env_file", ctx)
        result["per_user_env_name"] = f".env.{user_name}.{label}"
        result["hostname"] = f"{service_name}-{user_name}-{label}.{domain}"
        return result

    def _build_prompt(self, config_type: str, context: dict) -> str:
        """Build a prompt for config generation (legacy single-shot path)."""
        from ..utils.skill_loader import get_compose_rules, get_nginx_rules, get_compose_template, get_nginx_template

        desc = context.get("repo_description", "an application")
        files = context.get("repo_files", [])
        port = context.get("port", 8000)
        lang = context.get("language", "unknown")
        framework = context.get("framework", "unknown")

        if config_type == "docker_compose":
            compose_rules = get_compose_rules()
            compose_template = get_compose_template()
            template_block = ""
            if compose_template:
                template_block = f"\nReference template (fill in placeholders):\n```yaml\n{compose_template}\n```\n"
            return f"""Generate a docker-compose.yml for {desc}

Context:
- Language: {lang}
- Framework: {framework}
- Port: {port}
- Files in repo: {', '.join(files[:20])}
- Needs database: {context.get('needs_db', False)}
- Needs cache: {context.get('needs_cache', False)}

The generated file will be used by the provision tool (provision-api).
Follow these rules EXACTLY:

{compose_rules}
{template_block}
Output ONLY the raw YAML, no markdown fences, no explanations."""

        elif config_type == "nginx_conf":
            nginx_rules = get_nginx_rules()
            nginx_template = get_nginx_template()
            compose_services = context.get("compose_services", [])
            compose_hint = ""
            if compose_services:
                compose_hint = (
                    f"\nCRITICAL: The docker-compose.yml defines these service(s): {', '.join(compose_services)}.\n"
                    f"Your proxy_pass MUST use one of these exact service names as the host.\n"
                    f"For example: proxy_pass http://{compose_services[0]}:PORT;\n"
                )
            else:
                compose_hint = (
                    "\nCRITICAL: You MUST determine the service name from docker-compose.yml.\n"
                    "The proxy_pass host must match a compose service key exactly, or registration will be rejected.\n"
                )
            template_block = ""
            if nginx_template:
                template_block = f"\nReference template (fill in placeholders):\n```nginx\n{nginx_template}\n```\n"
            return f"""Generate an nginx reverse proxy configuration for {desc}

Context:
- App port: {port}
- Service will be behind provision-nginx
- Need basic auth support
- Files in repo: {', '.join(files[:15])}{compose_hint}
The generated file will be used by the provision tool (provision-api).
Follow these rules from the provision-api skill:

{nginx_rules}
{template_block}
Output ONLY the raw nginx config, no markdown fences."""

        elif config_type == "env_file":
            return f"""Generate a .env file template for {desc}

Include sensible defaults for:
- APP_PORT={port}
- APP_ENV=production
- Any database connection strings if needed
- Any cache connection strings if needed
- LOG_LEVEL=info

Use ${{VAR}} syntax for values that should be customized.
Output ONLY the raw env file."""

        elif config_type == "dockerfile":
            return f"""Generate a Dockerfile for {desc}

Context:
- Language: {lang}
- Framework: {framework}
- Port: {port}
- Files: {', '.join(files[:10])}

Requirements:
- Use a slim base image appropriate for {lang}
- Set WORKDIR /app
- Copy dependency files first for layer caching
- Install dependencies
- Copy application code
- EXPOSE {port}
- Use a non-root user if possible
- Include a HEALTHCHECK if possible

Output ONLY the raw Dockerfile, no markdown fences."""

        return f"Generate a {config_type} for {desc}"

    def _extract_code_block(self, content: str, config_type: str) -> str:
        """Extract YAML/code blocks from LLM response."""
        fence_lang = {
            "docker_compose": "yaml",
            "nginx_conf": "nginx",
            "env_file": "bash",
            "dockerfile": "dockerfile",
        }.get(config_type, "")

        pattern = rf"```(?:{fence_lang})?\s*\n(.*?)```"
        matches = re.findall(pattern, content, re.DOTALL)
        if matches:
            return matches[0].strip()

        if config_type == "docker_compose":
            m = re.search(r"(version:|services:|name:)", content)
            if m:
                return content[m.start():].strip()
        elif config_type == "nginx_conf":
            m = re.search(r"(server\s*\{|upstream\s+)", content)
            if m:
                return content[m.start():].strip()
        elif config_type == "dockerfile":
            m = re.search(r"(FROM\s+\S+)", content, re.IGNORECASE)
            if m:
                return content[m.start():].strip()

        return content.strip()


# Singleton
llm_service = LLMService()
