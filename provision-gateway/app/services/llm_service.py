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

# When the tool loop (or a self-repair response) ends with tool calls but no
# file content, force ONE content-only call so the accumulated tool results
# turn into an answer instead of failing the phase with "empty output".
FINALIZE_PROMPT = (
    "Stop using tools. Output the COMPLETE requested file now — ONLY the raw "
    "file content, no markdown fences, no explanations."
)
WEB_FETCH_MAX_BYTES = 64 * 1024      # per-page size cap
TOTAL_FETCH_MAX_BYTES = 512 * 1024   # total fetched bytes cap
FETCH_TIMEOUT = 30.0
MAX_BASE_FILE_BYTES = 128 * 1024

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



def _split_removed_report(kvs: Any) -> tuple[Any, Any]:
    """Pull the model's self-reported adversarial removals out of its answer.

    Turn 2 asks for an extra ``_removed`` key: the variables it judged
    platform-owned, each with a one-line reason. That key carries no
    ``env_type``, so the env filter would discard it — this keeps the reasons so
    they can be logged, making the model's filtering auditable without the
    gateway pattern-matching variable names.

    Returns ``(kvs_without_the_key, report_or_None)``.
    """
    if isinstance(kvs, dict) and "_removed" in kvs:
        kvs = dict(kvs)          # don't mutate the caller's parsed object
        return kvs, kvs.pop("_removed")
    return kvs, None


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
            reasoning_effort=str(data.get("reasoning_effort") or "low"),
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
        # Generation behavior settings (design §2 LLM settings).
        if "reasoning_effort" in data:
            config.reasoning_effort = str(data["reasoning_effort"]) or "low"
        if "max_tokens" in data:
            try:
                config.max_tokens = int(data["max_tokens"])
            except (TypeError, ValueError):
                config.max_tokens = 100000
        if "tool_rounds_max" in data:
            try:
                config.tool_rounds_max = max(1, min(int(data["tool_rounds_max"]), 20))
            except (TypeError, ValueError):
                config.tool_rounds_max = 6
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
        self._active_config = db.query(LLMConfig).filter(LLMConfig.is_active == True).first()
        config = self._active_config

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

    def _behavior_settings(self) -> dict:
        """Generation behavior settings from the active config (design §2).

        reasoning_effort / max_tokens / tool_rounds_max with defaults when the
        config row is missing or legacy.
        """
        cfg = getattr(self, "_active_config", None)
        rounds = (cfg.tool_rounds_max if cfg else None) or 6
        return {
            # default to the LOWEST reasoning effort (thinking still enabled) —
            # env/agent turns that only need a short answer stay fast (design §2).
            "reasoning_effort": (cfg.reasoning_effort if cfg else None) or "low",
            "max_tokens": (cfg.max_tokens if cfg else None) or 100000,
            "tool_rounds_max": max(1, min(int(rounds), 20)),
        }

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
        from ..utils.skill_loader import get_compose_rules, get_nginx_rules, get_serving_ports_rules, get_compose_template, get_nginx_template

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
        # The env phase prints its own structured deployment_meta handoff (see
        # the env_file branch) — keep the generic block for compose/nginx.
        if meta_lines and config_type != "env_file":
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
            # R1.5 / T16 — the EXTRA http endpoints this deployment declares.
            #
            # R1.3 makes it an ERROR for a surviving `kind: http` row to have no
            # `server{}` block serving `<instance host>--<sub_service>.<domain>`,
            # and the serving_ports artifact is where those rows live. Without
            # this the model saw only the compose, wrote ONE block, and every
            # generation failed its own validation (measured 2026-10-10, jobs
            # 366/367). The artifact is authoritative here for the same reason it
            # is in the serving_ports phase: it is the deployment's own
            # declaration of what exists.
            extras_block = ""
            _sp_path = context.get("serving_ports_path")
            if _sp_path:
                try:
                    import yaml as _yaml

                    from .serving_ports import http_rows as _http_rows

                    _rows = _http_rows(
                        _yaml.safe_load(Path(_sp_path).read_text(encoding="utf-8", errors="replace"))
                    )
                    _extra = [
                        r for r in _rows
                        if r.get("primary") is not True and r.get("sub_service")
                    ]
                    if _extra:
                        _lines = "\n".join(
                            "  - server_name {service_name}_hostname--%s;  with proxy_pass "
                            "http://%s:%s;" % (r["sub_service"], r["sub_service"], r["port"])
                            for r in _extra
                        )
                        extras_block = (
                            "\nThis deployment declares these ADDITIONAL http endpoints "
                            "(from its serving_ports artifact — one `server { listen 80; … }` "
                            "block EACH, server_name and proxy_pass exactly as shown, and no "
                            "other extra blocks):\n" + _lines
                        )
                except Exception:  # noqa: BLE001 — a missing artifact must not fail the prompt
                    extras_block = ""
            parts.append(f"""
Generate an nginx reverse proxy configuration. It sits behind provision-nginx,
serving hostname {deploy_meta.get('domain','localhost')} with basic auth support.{hint}{extras_block}
Follow these rules from the provision-api skill:

{rules}
{template_block}
Output ONLY the raw nginx config, no markdown fences.""")

        elif config_type == "serving_ports":
            # T13 / R1.2. Two blocks, and the difference between them matters:
            # the RULES come from the platform's skill file (where the compose and
            # nginx rules live), and the PORT SURFACE comes from the compose
            # actually in play — the prompt's own ground truth of what must have
            # a row. The application/port table inside the rules is explicitly
            # reference-only, because a port number is not authority for what
            # speaks on it.
            from ..services.serving_ports import load_kind_categories, compose_port_surface

            rules = get_serving_ports_rules()
            table = load_kind_categories()
            kinds_block = "\n".join(
                f"  {k} -> {v}" for k, v in sorted((table.get("kinds") or {}).items())
            )
            surface = compose_port_surface([Path(p) for p in (context.get("compose_paths") or [])])
            surface_block = ""
            if surface:
                rows = "\n".join(
                    f"  - {r['sub_service']} (from `{r['source']}:`): container port {r['port']}"
                    for r in surface
                )
                surface_block = (
                    "\nEvery port the compose in play mentions, mechanically scanned. Each one MUST\n"
                    "appear as a row: this list is what exists, and a missing row leaves the operator\n"
                    "unable to tell whether the analysis missed the port or the platform refuses it:\n"
                    + rows + "\n"
                )
            parts.append(f"""
The platform's kind -> category table, the ONLY load-bearing mapping. A kind not
listed here takes the `else` category (`{table.get("else")}`):
{kinds_block}
{surface_block}
{rules}

Output ONLY the raw YAML, no markdown fences, no commentary.""")

        elif config_type == "env_file":
            meta_bits = f" for user '{deploy_meta.get('user_name','?')}' label {deploy_meta.get('label','0')} on {deploy_meta.get('domain','localhost')}"
            handoff = (
                "  serving_url: " + str(deploy_meta.get("serving_url") or "") + "\n"
                "  scheme: " + str(deploy_meta.get("scheme") or "http") + "\n"
                "  hostname: " + str(deploy_meta.get("hostname") or "") + "\n"
                "  http_port: " + str(deploy_meta.get("http_port") or "") + "\n"
                "  https_port: " + str(deploy_meta.get("https_port") or "") + "\n"
                "  service_name: " + str(deploy_meta.get("service_name") or "") + "\n"
                "  user_name: " + str(deploy_meta.get("user_name") or "") + "\n"
                "  label: " + str(deploy_meta.get("label") or "0")
            )
            parts.append(f"""
Generate the per-user .env (docker compose ${'{VAR}'} interpolation env){meta_bits}.

The base files named like *.env / *.example document each variable — READ their
comment lines to understand what each variable is FOR before you fill anything.
Do not assume meaning from the variable name alone.

deployment_meta — authoritative, these are the values the deployed instance is
served on (the gateway assembled them from the -api naming + edge ports):
{handoff}

Every variable the instance needs in order to be reachable and usable by a
browser/operator MUST be filled from deployment_meta, never left at an empty or
localhost/example placeholder. Follow these rules EXACTLY, then output NOW:
1. Every REQUIRED variable in __compose_variables__.txt must be present.
2. Identify by READING the base-file comments the variables whose meaning is the
   instance's externally-reachable identity — a serving/entry URL, origin, host,
   API/console/web base, login/callback/redirect/webhook or websocket URL, or a
   "localhost"-style placeholder. Fill each from deployment_meta: an absolute
   origin/URL from serving_url; a bare host from hostname; a sub-path only when
   the variable's meaning requires it (e.g. origin + "/console/api"). Keep the
   scheme consistent with deployment_meta.scheme.
3. Secrets (SECRET_KEY, password, key, token, secret, salt, credential): a fresh
   random value.
4. Internal service-to-service addresses (compose service names such as
   "http://api:5001", or plain :port inside the compose network) stay unchanged.
5. Other variables keep their example value; never invent values the deployment
   does not need.
6. Output KEY=VALUE lines for the variables you set plus the REQUIRED/OPTIONAL
   compose variables. No markdown fences, no comments, no explanations.
   Answer with the file content immediately.""" + (
                ('\nActivated profiles: ' + ', '.join(profiles) + ' — the env must match the services those profiles gate (e.g. a postgresql profile needs a working DATABASE_URL).') if profiles else ''
            ))

        else:
            parts.append(f"\nGenerate the {config_type} file for {desc}.")

        return "\n".join(parts)


    def _rec_usage_live(self, resp: dict, usage_cb=None) -> None:
        """Record usage AND push the running totals to the job runner so the
        Deploying… popup can show live in/out/rsn counters per step."""
        self._record_usage(resp)
        if usage_cb:
            try:
                usage_cb(dict(self._usage_total))
            except Exception:
                pass

    def _record_usage(self, resp: dict) -> None:
        """Accumulate token usage into the current generation's totals."""
        total = getattr(self, "_usage_total", None)
        if total is None:
            return
        u = (resp or {}).get("usage") or {}
        total["prompt"] += int(u.get("prompt_tokens") or 0)
        total["completion"] += int(u.get("completion_tokens") or 0)
        det = u.get("completion_tokens_details") or {}
        total["reasoning"] += int(det.get("reasoning_tokens") or 0)

    # ------------------------------------------------------------------
    # Env fill — two-turn conversation (design §2 env redesign)
    # ------------------------------------------------------------------
    # Turn 1: model reads the env base files (comment lines = variable meaning)
    # + the compose ${VAR} surface and identifies the variables that MUST be
    # given concrete values. NO deployment_meta is shown.
    # Turn 2 (same conversation): deployment_meta is appended and the model
    # returns a JSON object mapping those variables to their final values.
    # The gateway assembles the .env programmatically from that map over the
    # mechanical merge — the model never emits the whole file.

    @staticmethod
    def _json_first(text: str, want_object: bool) -> Any:
        """Parse a JSON array (want_object=False) or object (True) out of a
        model reply, tolerating markdown fences / prose around it."""
        import json as _json
        import re as _re

        text = (text or "").strip()
        text = _re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
        try:
            data = _json.loads(text)
            ok = isinstance(data, dict) if want_object else isinstance(data, list)
            return data if ok else None
        except Exception:
            pass
        pat = r"\{[\s\S]*\}" if want_object else r"\[[\s\S]*\]"
        m = _re.search(pat, text)
        if not m:
            return None
        try:
            return _json.loads(m.group(0))
        except Exception:
            return None

    # ==================================================================
    # ENV PROMPTS — READ THIS BEFORE ADDING A VARIABLE NAME
    # ==================================================================
    # The two env prompts below name a handful of variables
    # (COOKIE_DOMAIN, COMPOSE_PROFILES, CERTBOT_DOMAIN, CONSOLE_API_URL,
    # SECRET_KEY). Every one of them is an ILLUSTRATION of a class, or part of
    # a JSON shape example. None of them is a rule, and no code branches on any
    # of them — the model decides, the gateway does not pattern-match.
    #
    # When a new variable-shaped failure turns up, the fix is to extend the
    # SYSTEM MODEL (what the platform owns and why) and let the model re-derive
    # which variables that covers. Adding the new name to a list here is the
    # move that FAILED: `cookie domain` was named in the necessary list, so the
    # model filled COOKIE_DOMAIN and the scope flipped between deploys (broke
    # sign-in); CERTBOT_DOMAIN then got filled for the same reason (this service
    # never terminates TLS); COMPOSE_PROFILES was dropped from some runs and
    # silently replaced the deploy panel's recorded choice. Each was patched
    # with a name check in code, and each patch was reverted — name lists do not
    # generalise, and a name match cannot tell a platform-owned variable from an
    # app one that merely looks like it.
    #
    # So: state the property ("would this restate a decision the platform has
    # already made?"), give the reason, and let the model judge meaning from the
    # base-file comments. Turn 2 self-reports its removals under `_removed`
    # (see _split_removed_report) so the judgement is auditable; if it gets one
    # wrong, sharpen the reasoning — do not add a name check.
    # ==================================================================

    def _env_turn1_prompt(self, context: dict) -> str:
        """Turn-1 prompt: identify which variables need concrete values.
        Deliberately excludes deployment_meta so identification is unbiased."""
        dm = context.get("deploy_metadata") or {}
        parts: list[str] = []
        parts.append(
            f"Do NOT write any file. Identify which environment variables must be "
            f"given concrete values so this deployed instance (service "
            f"{context.get('repo_description','?')}, user "
            f"'{dm.get('user_name','')}' label {dm.get('label','0')}) works."
        )
        # How the platform actually serves this instance. Without it the model can
        # only guess whether the service terminates its own TLS / owns its own
        # cookie scope / chooses its own optional services — and it guessed wrong:
        # it filled COOKIE_DOMAIN (broke sessions across redeploys), certbot's
        # CERTBOT_DOMAIN (this service never terminates TLS), and would fill
        # COMPOSE_PROFILES (the deploy panel owns that). State the model of the
        # system; the variable-level conclusions then follow from it.
        parts.append(
            "How THIS PLATFORM serves the instance (authoritative — reason from "
            "this, do not assume a normal standalone deployment):\n"
            "- Requests reach it ONLY through the platform's own edge, at the "
            "single hostname assigned to this instance. The edge terminates TLS "
            "with the PLATFORM's certificate and forwards plain HTTP into the "
            "compose network. This service's nginx therefore listens on plain "
            "HTTP and needs no certificate: nothing here obtains one (no "
            "ACME/certbot), and when an operator wants HTTPS they give the "
            "platform the certificate.\n"
            "- That hostname is fixed per (service, user, label) and is REUSED "
            "across redeploys of the same instance.\n"
            "- Which optional services exist is decided in the deploy panel and "
            "passed to compose as --profile, then recorded by the platform.\n"
            "- So three kinds of variable are the PLATFORM's, not the app's, and "
            "must be left exactly as the base files have them: anything that "
            "scopes cookies to a domain, anything that activates compose "
            "profiles, and anything that configures obtaining a TLS certificate."
        )
        base_files = context.get("base_files") or {}
        blocks = []
        for fname, content in base_files.items():
            if fname.startswith("__mechanical_env__"):
                continue  # KEY=VALUE-only duplicate — no comment meaning to read
            blocks.append(f"--- base file: {fname} ---\n{content}")
        if blocks:
            parts.append(
                "The following files document every variable. READ the comment "
                "lines to learn each variable's meaning before deciding:\n\n"
                + "\n\n".join(blocks)
            )
        parts.append(
            "Identify the NECESSARY variables — and ONLY those. A variable is "
            "necessary iff:\n"
            "1. Its meaning (from the comments) is the instance's "
            "externally-reachable identity affected by THIS deployment — "
            "serving/entry URL, origin, host, API/console/web/ws BASE, "
            "login/callback/redirect/webhook URL. These are "
            "filled from deployment_meta, overriding any existing value.\n"
            "2. It is REQUIRED by the compose (no default) AND currently EMPTY — "
            "the instance cannot boot without it.\n"
            "\n"
            "For every variable you identify, also decide WHICH OF THREE "
            "CATEGORIES it falls into — where its value can legitimately come "
            "from:\n"
            "  · \"non-secret\" — a deployment-derived value: the platform knows "
            "it (a URL/origin/host/base built from deployment_meta). The "
            "deployment fills it in.\n"
            "  · \"secret\" — a secret THIS INSTANCE generates for itself, so a "
            "value invented here and now is still CORRECT: a signing key, an "
            "encryption key, a database password for a database this compose "
            "creates, a session salt. Nobody outside needs to know it, and the "
            "same random value every deploy is not required.\n"
            "  · \"external\" — a credential issued by a THIRD PARTY that this "
            "instance merely consumes: an API key or account token for an "
            "external service (a model provider, a mail relay, an analytics or "
            "object-storage account). The test that separates it from "
            "\"secret\": could a value invented here and now be correct? For a "
            "self-generated key yes; for a credential someone else must issue "
            "no — inventing one produces a plausible-looking string that fails "
            "at first use. Those the OPERATOR supplies; say so with this "
            "category and DO NOT invent a placeholder value.\n"
            "NOT necessary — and therefore EXCLUDED:\n"
            "- Anything the PLATFORM owns (see 'How THIS PLATFORM serves the "
            "instance' above), judged by MEANING — what the variable DOES — "
            "never by its spelling:\n"
            "  · a cookie's domain/scope. The platform assigns one hostname per "
            "instance and reuses it, so the scope must be identical on every "
            "deploy: set once and not the next time, a browser keeps sending a "
            "stale cookie from the destroyed instance that shadows the fresh "
            "one, and the app then rejects every logged-in request while its "
            "login call still returns success.\n"
            "  · compose profile activation (e.g. COMPOSE_PROFILES). The deploy "
            "panel decides it, records it and passes `--profile`; a value here "
            "silently OVERRIDES that recorded choice.\n"
            "  · obtaining a TLS certificate (e.g. an ACME/certbot domain, "
            "contact email or challenge setting). This service does not "
            "terminate TLS — the platform's edge does, with the platform's "
            "certificate, so nothing in this deployment requests one.\n"
            "- Any password/secret/credential/SECRET_KEY/token/key that ALREADY "
            "carries a value (app defaults are left untouched — they are "
            "internally consistent and must NOT be regenerated).\n"
            "- Internal service-to-service addresses (compose service names / "
            ":port inside the compose network).\n"
            "- Anything with a safe compose default that is not deployment-"
            "affected.\n"
            'Reply with ONLY a JSON object mapping each necessary variable to '
            'its category, e.g. '
            '{"CONSOLE_API_URL":"non-secret","SECRET_KEY":"secret",'
            '"OPENAI_API_KEY":"external"}. No markdown fences, no prose.'
        )
        return "\n".join(parts)

    def _env_turn2_prompt(
        self, context: dict, var_list: list[str], operator_prompt: str = "",
        categories: dict | None = None,
    ) -> str:
        """Turn-2 prompt (appended to the same conversation): map the
        identified variables to concrete values from deployment_meta.

        ``categories`` carries turn 1's name → category verdict, so each variable
        is shown with the category it was classified into. The model does not
        re-litigate the categories here; it echoes ``external`` ones with a null
        value for the operator to fill, and never invents a placeholder.
        """
        dm = context.get("deploy_metadata") or {}
        handoff = (
            "  serving_url: " + str(dm.get("serving_url") or "") + "\n"
            "  scheme: " + str(dm.get("scheme") or "http") + "\n"
            "  hostname: " + str(dm.get("hostname") or "") + "\n"
            "  http_port: " + str(dm.get("http_port") or "") + "\n"
            "  https_port: " + str(dm.get("https_port") or "") + "\n"
            "  service_name: " + str(dm.get("service_name") or "") + "\n"
            "  user_name: " + str(dm.get("user_name") or "") + "\n"
            "  label: " + str(dm.get("label") or "0")
        )
        names = "\n".join(
            f"- {n}" + (f"   [{categories[n]}]" if categories and categories.get(n) else "")
            for n in var_list
        )
        operator_block = ""
        if operator_prompt and operator_prompt.strip():
            operator_block = (
                "\n\nOperator prompt (AUTHORITATIVE when present — if it states "
                "exact URL/API/console rules or points at the app's docs, follow "
                "them EXACTLY; these OVERRIDE the default-origin rule below). "
                "Where it is silent, keep the defaults below.\n"
                "--- operator prompt / docs ---\n"
                f"{operator_prompt.strip()}\n"
                "--- end operator prompt ---"
            )
        return (
            "deployment_meta — authoritative values this instance is served on "
            "(assembled by the gateway from the -api naming + edge ports):\n"
            f"{handoff}\n\n"
            "Now assign the final value for each variable you identified. Recall "
            "each variable's meaning from the base files you read.\n"
            f"Variables to fill:\n{names}\n{operator_block}\n\n"
            "Rules (defaults — overridden only by the operator prompt above):\n"
            "- A variable whose meaning is the instance's externally-reachable "
            "identity (URL/origin/host, or an API/console/web/ws BASE) gets its "
            "value FROM deployment_meta: a BASE variable equals serving_url "
            "(the ORIGIN) EXACTLY — the app appends its own path prefix (e.g. "
            "/console/api) to that base itself, so NEVER append /console/api, "
            "/api or any API path to it. Use a bare host from hostname when the "
            "variable is only the host. Only set a full URL that carries a "
            "sub-path when the variable's meaning is an explicit endpoint or "
            "template (e.g. a URL_TEMPLATE) — derive it from serving_url. Never "
            "leave it empty or at a localhost/example placeholder.\n"
            "- For a variable in the list whose MEANING is a SECRET (password, "
            "key, token, secret, salt, credential): env_type is 'secret' and "
            "value is null — the gateway mints a PLAIN random value. (Only "
            "EMPTY required secrets appear in the list; secrets that already "
            "carry a value are never listed.)\n"
            "- BEFORE YOU ANSWER, re-read the list above and REMOVE every "
            "variable that belongs to the PLATFORM rather than to the app.\n"
            "  THE TEST — apply it to each variable in the list, and to ones I "
            "have not thought of: would setting it RESTATE A DECISION THE "
            "PLATFORM HAS ALREADY MADE for this instance? The platform decided, "
            "and recorded elsewhere, (a) the one hostname this instance is "
            "served on and anything scoped to it, (b) which optional services "
            "are activated, and (c) which certificate the edge presents. It "
            "applied those decisions when it deployed you; a second copy in "
            "this env is redundant at best and DISAGREES at worst.\n"
            "  The three cases I know of, and why each is the platform's: a "
            "cookie's domain/scope — a browser then keeps a stale cookie from "
            "the destroyed instance that shadows the fresh one, so the app "
            "rejects every logged-in request while its login call still "
            "returns success; compose profile activation — the deploy panel "
            "records it and passes `--profile`, so a value here silently "
            "overrides the recorded choice; anything for obtaining a TLS "
            "certificate (ACME/certbot domain, contact, challenge) — this "
            "service does not terminate TLS, the platform's edge does.\n"
            "  Judge by MEANING — what the variable DOES, from the base-file "
            "comments — never by its spelling. The list may well contain one "
            "that slipped through identification. Drop each from your JSON "
            "ENTIRELY: do not give it a value, do not set it to empty, do not "
            "carry it over.\n"
            "- For a variable you listed as a THIRD-PARTY CREDENTIAL this "
            "instance merely consumes (marked [external] above): env_type is "
            "'external' and value is null. The operator supplies it — the "
            "gateway pauses and asks. NEVER invent a placeholder for it, do not "
            "guess a plausible format (no 'sk-...', no 'your-key-here'), and do "
            "not carry an example value over: a made-up credential looks "
            "configured and fails at first use, which is worse than an empty "
            "one.\n"
            "- For every OTHER variable: env_type is 'non-secret' and value is "
            "the final value (from deployment_meta / derivation as above).\n"
            "- Keep values consistent with the compose network for internal "
            "addresses if any are in the list.\n"
            "Reply with ONLY a JSON object mapping each variable to "
            '{"env_type":"secret"|"non-secret"|"external","value":<string|null>}, e.g. '
            '{"SECRET_KEY":{"env_type":"secret","value":null},'
            '"CONSOLE_API_URL":{"env_type":"non-secret",'
            '"value":"http://dify-x.localhost:8767"},'
            '"OPENAI_API_KEY":{"env_type":"external","value":null}}. '
            'Plus exactly one extra key, "_removed": an object mapping each '
            "variable you removed above to a one-line reason naming the "
            'platform decision it would have restated, e.g. {"_removed":'
            '{"COOKIE_DOMAIN":"restates the hostname the platform serves this '
            'instance on"}}. Use {} when you removed nothing. That key is the '
            "audit trail for the removal step — the reason must be specific "
            "enough that a reviewer can disagree with your judgement. "
            "No markdown fences, no comments, no other extra keys."
        )

    def _fix_analysis_prompt(self, context: dict) -> str:
        """The ANALYSIS prompt for `LLM based fix` (GRILLING G26).

        Asks for a FIXING GUIDE — the guidance an operator would type into the
        pause popup — never for corrected files. The guide is what the restarted
        path consumes; the `.fix` files come from that restart (G25), so this
        step writing files would be writing them twice and outside the gate.
        """
        failure = context.get("failure") or {}
        log = context.get("task_log") or ""
        files = context.get("base_files") or {}
        meta = context.get("deploy_metadata") or {}

        parts: list[str] = []
        parts.append(
            "A deployment failed. Analyse WHY it failed and write a FIXING GUIDE "
            "for the generation step that will rebuild this deployment's files.\n"
        )
        parts.append(
            "The guide is a DIRECTIVE addressed to that generation step: state "
            "what must be changed in the deployment files and why, in enough "
            "detail that the correction is unambiguous. Do NOT write the files "
            "themselves, and do not answer with a diff — answer with the "
            "instruction.\n"
        )
        parts.append(
            "Also name which of the three slots is at fault, as a single word "
            "on a line of its own, exactly one of:\n"
            "SLOT: compose\nSLOT: nginx\nSLOT: env\nSLOT: unknown\n"
        )
        parts.append(
            "Deployment: service=%s user=%s label=%s domain=%s"
            % (
                context.get("service_name") or "",
                meta.get("user_name") or context.get("user_name") or "",
                meta.get("label") or context.get("label") or "0",
                meta.get("domain") or "localhost",
            )
        )
        if failure:
            parts.append(
                "Failure stage: %s\nFailure message:\n%s"
                % (failure.get("stage") or "(unknown)", failure.get("message") or "(none)")
            )
        if files:
            listing = []
            for name, body in files.items():
                listing.append(
                    "--- %s ---\n%s" % (name, (body or "")[:6000])
                )
            parts.append(
                "The deployment files currently in the recipe:\n\n" + "\n\n".join(listing)
            )
        # The log is fed WHOLE (llm-based-fix-todo.md §4): a truncated tail is
        # what hides the first error, and the first error is usually the cause.
        parts.append("The failed task's log, in full:\n\n" + (log or "(empty log)"))
        parts.append(
            "\nAnswer with the guide, then the SLOT line. No preamble, no "
            "markdown fences around the whole answer."
        )
        return "\n\n".join(parts)

    @staticmethod
    def _split_fix_guide(content: str) -> tuple[str, str]:
        """Split the analysis answer into ``(guide, slot)``.

        `slot` is a best-effort hint used to default WHICH slot the restart sets
        to `generate new` — the operator sees and can change it, so an unusable
        answer falls back to `compose` rather than failing the step.
        """
        import re as _re

        text = content or ""
        slot = ""
        m = _re.search(r"^\s*SLOT\s*:\s*(compose|nginx|env|unknown)\s*$",
                       text, _re.IGNORECASE | _re.MULTILINE)
        if m:
            slot = m.group(1).strip().lower()
            text = text[:m.start()] + text[m.end():]
        return text.strip(), (slot or "compose")

    async def generate_fix_guide(
        self,
        db: Session,
        context: dict,
        progress=None,
        session_log=None,
        usage_cb=None,
        on_delta=None,
    ) -> dict:
        """Analyse a failed deployment → ``{"guide", "slot", "usage", "warnings"}``.

        No files are written: the only output is the guide (GRILLING G26).
        """
        base_url, model, headers = self._resolve_endpoint(db)
        cfg = db.query(LLMConfig).filter(LLMConfig.is_active == True).first()
        sysp = (cfg.system_prompt if cfg and cfg.system_prompt else "") or ""
        usage_total: dict[str, int] = {"prompt": 0, "completion": 0, "reasoning": 0}

        messages: list[dict] = []
        if sysp:
            messages.append({"role": "system", "content": sysp})
        prompt = self._fix_analysis_prompt(context)
        messages.append({"role": "user", "content": prompt})

        if progress:
            await progress("analyzing the failure to write a fixing guide")
        resp = await self._chat(base_url, model, headers, messages, on_delta=on_delta)
        u = (resp or {}).get("usage") or {}
        usage_total["prompt"] += int(u.get("prompt_tokens") or 0)
        usage_total["completion"] += int(u.get("completion_tokens") or 0)
        usage_total["reasoning"] += int(
            (u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
        )
        if usage_cb:
            try:
                usage_cb(dict(usage_total))
            except Exception:
                pass
        choice = ((resp.get("choices") or [{}])[0].get("message") or {})
        content = choice.get("content") or ""
        if session_log:
            session_log({
                "stage": "fix.analyze",
                "finish_reason": (resp.get("choices") or [{}])[0].get("finish_reason"),
                "prompt_head": prompt[:1500],
                "content_head": content[:3000],
                "reasoning": (choice.get("reasoning_content") or "")[:3000],
                "usage": dict(usage_total),
            })
        guide, slot = self._split_fix_guide(content)
        warnings: list[str] = []
        if not guide:
            warnings.append("the analysis returned no guide text")
        return {"guide": guide, "slot": slot, "usage": usage_total, "warnings": warnings}

    async def generate_env_two_turn(
        self,
        db: Session,
        context: dict,
        progress=None,
        session_log=None,
        usage_cb=None,
        on_delta=None,
    ) -> dict:
        """Run the two-turn env conversation. Returns
        ``{"vars": [...], "kvs": {...}, "warnings": [...], "usage": {...}}``.
        ``kvs`` empty ⇒ caller falls back to the mechanical env.
        """
        import json as _json

        base_url, model, headers = self._resolve_endpoint(db)
        cfg = db.query(LLMConfig).filter(LLMConfig.is_active == True).first()
        sysp = (cfg.system_prompt if cfg and cfg.system_prompt else "") or ""
        usage_total: dict[str, int] = {"prompt": 0, "completion": 0, "reasoning": 0}

        def _add(resp: dict) -> None:
            u = (resp or {}).get("usage") or {}
            usage_total["prompt"] += int(u.get("prompt_tokens") or 0)
            usage_total["completion"] += int(u.get("completion_tokens") or 0)
            det = u.get("completion_tokens_details") or {}
            usage_total["reasoning"] += int(det.get("reasoning_tokens") or 0)
            # Live token counters: hand the running totals to the caller after
            # EVERY LLM call (the popup shows them per step while it runs).
            if usage_cb:
                try:
                    usage_cb(dict(usage_total))
                except Exception:
                    pass

        def _cap(s: str, n: int = 30000) -> str:
            s = s or ""
            return s if len(s) <= n else s[:n] + f"…[+{len(s) - n} chars]"

        def _log(entry: dict) -> None:
            if session_log:
                entry.setdefault("usage", dict(usage_total))
                session_log(entry)

        messages: list[dict] = []
        if sysp:
            messages.append({"role": "system", "content": sysp})

        # ---- Turn 1: identify necessary variables (no deployment_meta) ----
        user1 = self._env_turn1_prompt(context)
        messages.append({"role": "user", "content": user1})
        if progress:
            await progress("env: identifying variables that need values (turn 1/2)")
        resp1 = await self._chat(base_url, model, headers, messages, on_delta=on_delta)
        _add(resp1)
        c1 = ((resp1.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        _m1 = ((resp1.get("choices") or [{}])[0].get("message") or {})
        _log({
            "stage": "env.turn1",
            "finish_reason": ((resp1.get("choices") or [{}])[0].get("finish_reason")),
            "prompt_head": _cap(user1, 1500),
            "content_head": _cap(c1, 3000),
            "reasoning": _cap(_m1.get("reasoning_content") or ""),
        })
        # Turn 1 answers with {name: category}. A bare array (the pre-category
        # contract) is still accepted and treated as category-less, so a model
        # that regresses to the old shape does not fail the phase.
        turn1 = self._json_first(c1, want_object=True)
        if turn1 is None:
            turn1 = self._json_first(c1, want_object=False)
        if turn1 is None:
            # one strict-JSON nudge for turn 1
            messages.append({"role": "assistant", "content": c1[:2000]})
            messages.append({"role": "user", "content":
                "Your reply was not a JSON object. Reply with ONLY the JSON object mapping each "
                "variable name to its category: \"non-secret\", \"secret\" or \"external\"."})
            resp1b = await self._chat(base_url, model, headers, messages, on_delta=on_delta)
            _add(resp1b)
            c1 = ((resp1b.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            turn1 = self._json_first(c1, want_object=True)
        categories: dict[str, str] = {}
        if isinstance(turn1, dict):
            for k, v in turn1.items():
                name = str(k).strip()
                if not name:
                    continue
                cat = str(v).strip().lower() if not isinstance(v, dict) else ""
                categories[name] = cat if cat in ("non-secret", "secret", "external") else ""
        else:  # bare array — pre-category contract
            for x in (turn1 or []):
                if isinstance(x, str) and x.strip():
                    categories[str(x).strip()] = ""
        var_list = list(categories)
        if categories:
            _log({"stage": "env.turn1.categories", "categories": categories})
        # Surface turn 1's tokens the moment the call returns (the popup's step
        # row shows the counters live instead of waiting for turn 2 to start).
        if progress:
            await progress(
                f"env: turn 1 done — {len(var_list)} variable(s) identified"
                f" (in {usage_total['prompt']} / out {usage_total['completion']} / rsn {usage_total['reasoning']})"
            )

        warnings: list[str] = []
        if not var_list:
            warnings.append("env turn 1 returned no variables — falling back to mechanical env")
            _log({"stage": "env.turn1", "ok": False, "vars": []})
            return {"vars": [], "kvs": {}, "categories": {}, "external": [],
                    "warnings": warnings, "usage": dict(usage_total)}

        # assistant continuity into turn 2
        messages.append({"role": "assistant", "content": c1[:4000]})

        # ---- Turn 2: map variables → values with deployment_meta ----
        op_prompt = (context.get("prompt") or "").strip()
        op_prefetched = await self._prefetch_prompt_urls(op_prompt) if op_prompt else ""
        user2 = self._env_turn2_prompt(context, var_list, operator_prompt=op_prefetched or op_prompt,
                                       categories=categories)
        messages.append({"role": "user", "content": user2})
        if progress:
            await progress(f"env: mapping {len(var_list)} values from deployment_meta (turn 2/2)")
        resp2 = await self._chat(base_url, model, headers, messages, on_delta=on_delta)
        _add(resp2)
        c2 = ((resp2.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        _m2 = ((resp2.get("choices") or [{}])[0].get("message") or {})
        _log({
            "stage": "env.turn2",
            "finish_reason": ((resp2.get("choices") or [{}])[0].get("finish_reason")),
            "prompt_head": _cap(user2, 1500),
            "content_head": _cap(c2, 3000),
            "reasoning": _cap(_m2.get("reasoning_content") or ""),
        })
        kvs = self._json_first(c2, want_object=True)
        if not kvs:
            messages.append({"role": "assistant", "content": c2[:2000]})
            messages.append({"role": "user", "content": "Your reply was not a JSON object. Reply with ONLY the JSON object mapping each variable to its final value."})
            resp2b = await self._chat(base_url, model, headers, messages, on_delta=on_delta)
            _add(resp2b)
            c2 = ((resp2b.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            kvs = self._json_first(c2, want_object=True)
        # Turn 2 self-reports what its adversarial pass removed, under a
        # `_removed` key. Pull it out BEFORE the filter below (which would drop
        # it anyway — it carries no env_type) so the reasons are recorded
        # instead of silently discarded.
        kvs, _removed_report = _split_removed_report(kvs)
        # Values are {env_type: secret|non-secret|external, value: str|null}
        # objects (decision-4 schema + the `external` category). Keep entries
        # that carry the env_type marker — `external` MUST be among the accepted
        # values: the filter used to admit only secret/non-secret, which would
        # silently drop every operator-supplied credential and fall back to the
        # mechanical env with no trace of why.
        kvs = {
            str(k): v
            for k, v in (kvs or {}).items()
            if isinstance(v, dict) and v.get("env_type") in ("secret", "non-secret", "external")
        }
        # Which of them the operator must supply. Only names that turn 1
        # classified `external` AND that survived into the final map — a
        # variable the adversarial pass dropped is not asked for.
        external_vars = sorted(
            name for name, v in kvs.items() if v.get("env_type") == "external"
        )
        if not kvs:
            warnings.append("env turn 2 returned no JSON map — falling back to mechanical env")
        if progress:
            await progress(
                f"env: turn 2 done — {len(kvs)} value(s) mapped"
                f" (in {usage_total['prompt']} / out {usage_total['completion']} / rsn {usage_total['reasoning']})"
            )
        _log({
            "stage": "env.turn2", "ok": bool(kvs), "vars": var_list,
            # Never log an `external` value: at this point it is always null
            # (the operator supplies it later), but masking the key keeps that
            # invariant visible if the contract ever changes.
            "kvs_head": {k: ("<external>" if v.get("env_type") == "external" else v)
                         for k, v in list(kvs.items())[:5]},
            "external": external_vars,
            # Audit trail for the adversarial removal pass — what the model
            # judged platform-owned and why. Absent if it reported nothing.
            "removed": _removed_report,
        })
        if _removed_report:
            warnings.append(
                "env: model removed platform-owned variable(s): "
                + ", ".join(sorted(str(k) for k in _removed_report))
            )
        return {
            "vars": var_list, "kvs": kvs, "removed": _removed_report,
            "categories": categories, "external": external_vars,
            "warnings": warnings, "usage": dict(usage_total),
        }

    def _build_repair_prompt(self, validation_errors: list[str]) -> str:
        return (
            "The previous attempt FAILED mechanical validation with these errors:\n"
            + "\n".join(f"- {e}" for e in validation_errors)
            + "\n\nRevise the output to fix every error. Output ONLY the corrected raw file content, no markdown fences."
        )

    @staticmethod
    def _non_thinking_sibling(base_url: str, model: str) -> str | None:
        """The non-reasoning sibling model for a deepseek endpoint.

        ``deepseek-v4-flash`` emits ``reasoning_content`` and can loop in
        chain-of-thought until the whole token budget is exhausted
        (finish_reason=length with EMPTY content) — ``deepseek-chat`` answers
        directly. Returns None when the endpoint is not deepseek or the
        configured model is already the sibling.
        """
        if "deepseek" not in (base_url or "").lower():
            return None
        if not model or model == "deepseek-chat":
            return None
        return "deepseek-chat"

    async def _finalize_chat(
        self,
        base_url: str,
        model: str,
        headers: dict,
        messages: list[dict],
        warnings: list[str],
    ) -> str:
        """One content-only call after a tool-heavy response produced no file content.

        The tool loop / repair response may end with tool calls (or empty text)
        instead of the file — this turns the accumulated tool results into the
        answer. Returns the raw content (possibly empty on failure).
        """
        messages.append({"role": "user", "content": FINALIZE_PROMPT})
        try:
            resp = await self._chat(base_url, model, headers, messages)
        except Exception as exc:
            warnings.append(f"finalize call failed: {exc}")
            return ""
        if "_error" in resp:
            warnings.append(resp["_error"])
            return ""
        self._record_usage(resp)
        return ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""

    # ------------------------------------------------------------------
    # LLM round-trip (tool-capable)
    # ------------------------------------------------------------------

    @staticmethod
    def _est_tokens(text: str) -> int:
        """Rough token estimate for live progress (CJK ≈ 1 token/char, other
        text ≈ 1 token per 3.5 chars). Display only — never used for accounting."""
        if not text:
            return 0
        cjk = sum(1 for c in text if '\u3000' <= c <= '\u9fff' or '\uff00' <= c <= '\uffef')
        return int(cjk + (len(text) - cjk) / 3.5) + 1

    async def _chat_stream(
        self,
        base_url: str,
        headers: dict,
        payload: dict,
        on_delta=None,
    ) -> dict:
        """Streaming chat/completions that REASSEMBLES the deltas into the SAME
        dict shape the non-streaming path returns, so every caller is unchanged.

        ``on_delta(est_out_tokens)`` is called (throttled) while the response
        streams, for live progress display. Returns ``{"_error": ...}`` on any
        problem (no usage, empty stream, HTTP error) so the caller can fall back
        to the non-streaming call.
        """
        import time as _time

        body = dict(payload)
        body["stream"] = True
        # Ask for the usage block on the final chunk (OpenAI-compatible servers).
        body["stream_options"] = {"include_usage": True}

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: dict[int, dict] = {}
        finish_reason = None
        usage: dict | None = None
        chunks = 0
        last_report = 0.0

        async with httpx.AsyncClient(timeout=300.0) as client:
            async with client.stream(
                "POST", f"{base_url}/chat/completions", json=body, headers=headers
            ) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode(errors="replace")[:300]
                    return {"_error": f"LLM HTTP {resp.status_code}: {detail}"}
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        continue
                    chunks += 1
                    if obj.get("usage"):
                        usage = obj["usage"]
                    for choice in (obj.get("choices") or []):
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            content_parts.append(delta["content"])
                        if delta.get("reasoning_content"):
                            reasoning_parts.append(delta["reasoning_content"])
                        for tc in (delta.get("tool_calls") or []):
                            idx = int(tc.get("index") or 0)
                            slot = tool_calls.setdefault(
                                idx, {"id": "", "type": "function",
                                      "function": {"name": "", "arguments": ""}}
                            )
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            # name/arguments arrive FRAGMENTED — concatenate.
                            if fn.get("name"):
                                slot["function"]["name"] += fn["name"]
                            if fn.get("arguments"):
                                slot["function"]["arguments"] += fn["arguments"]
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                    if on_delta:
                        now = _time.monotonic()
                        if now - last_report >= 1.5:
                            last_report = now
                            est = self._est_tokens(
                                "".join(content_parts) + "".join(reasoning_parts)
                            )
                            try:
                                on_delta(est)
                            except Exception:
                                pass

        if chunks == 0:
            return {"_error": "stream produced no chunks"}
        if not usage:
            # No usage block → the numbers would silently read 0; let the caller
            # fall back to the non-streaming call (exact accounting).
            return {"_error": "stream returned no usage"}

        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_parts),
        }
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        if tool_calls:
            message["tool_calls"] = [tool_calls[k] for k in sorted(tool_calls)]
        return {
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": usage,
        }

    async def _chat(
        self,
        base_url: str,
        model: str,
        headers: dict,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 16000,
        stream: bool = True,
        on_delta=None,
    ) -> dict:
        """One chat/completions call; returns the raw response dict (or {}).

        ``max_tokens`` is generous on purpose: reasoning models (deepseek
        emits ``reasoning_content``) count chain-of-thought against the same
        budget — small caps truncate the REASONING and leave ``content`` empty
        (finish_reason=length), which surfaces as "empty output" phase
        failures. The default is overridden by the configurable token budget
        (design §2 LLM settings); the finalize-call fallback in
        :meth:`generate_with_agent` covers the remaining exhaustion cases.
        """
        behavior = self._behavior_settings()
        budget = int(behavior.get("max_tokens") or max_tokens)
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": budget,
            "temperature": 0.3,
        }
        if behavior.get("reasoning_effort"):
            payload["reasoning_effort"] = behavior["reasoning_effort"]
        if tools:
            payload["tools"] = tools
        # Stream by default: `_chat_stream` returns the SAME dict shape, so every
        # caller is unchanged; any streaming problem degrades to the exact
        # non-streaming call below (never breaks generation).
        if stream:
            try:
                out = await self._chat_stream(
                    base_url, headers, payload, on_delta=on_delta
                )
            except Exception as exc:  # noqa: BLE001 — degrade, never raise
                out = {"_error": f"stream failed: {exc}"}
            if out and "_error" not in out:
                return out
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
        self, db: Session, config_type: str, context: dict, progress=None, session_log=None,
        usage_cb=None, on_delta=None,
    ) -> dict:
        """Agentic generation with the closed tool allowlist + self-repair.

        ``context`` keys (all optional): repo_description, prompt, base_files
        {name: content}, manifests {name: content}, deploy_metadata,
        profiles, compose_service_names, compose_paths (for validation),
        project_dir, recipe_path, empty_recipe.

        ``session_log`` (optional): sync callable receiving per-call dicts
        (role, content_head, tools, usage, finish_reason) for the Tasks-page
        session record.

        Returns ``{"generated_content", "filename_suggestion", "warnings",
        "validation": {...}, "tool_rounds": int, "usage": {...}}``.
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

        rounds_cap = self._behavior_settings()["tool_rounds_max"]
        self._usage_total: dict[str, int] = {"prompt": 0, "completion": 0, "reasoning": 0}
        usage_total = self._usage_total

        def _cap(s: str, n: int = 30000) -> str:
            s = s or ""
            return s if len(s) <= n else s[:n] + f"…[+{len(s) - n} chars]"

        def _log(entry: dict) -> None:
            if session_log:
                entry.setdefault("usage", dict(usage_total))
                session_log(entry)

        def _log_resp(stage: str, resp: dict, extra: dict | None = None) -> None:
            """Session-log one assistant reply: finish reason + content head +
            full reasoning + usage (feature 5 full-log contract)."""
            choice = (resp.get("choices") or [{}])[0] or {}
            msg = choice.get("message") or {}
            entry: dict = {
                "stage": stage,
                "finish_reason": choice.get("finish_reason"),
                "content_head": _cap(msg.get("content") or "", 3000),
                "reasoning": _cap(msg.get("reasoning_content") or ""),
            }
            if extra:
                entry.update(extra)
            _log(entry)

        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        # Record the operator/generation prompt head once per agent session so
        # the Tasks log shows what each LLM session was asked (feature 5).
        _log({"stage": "start", "config_type": config_type,
              "prompt_head": _cap(user_prompt, 1500), "tools_enabled": config_type != "env_file"})

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
        early_length = False

        # Sibling-first for env (design §2 overflow avoidance): the
        # non-reasoning model answers the env task in one fast call — run it
        # BEFORE the reasoning chain; fall through to the full chain on failure.
        if config_type == "env_file":
            sib = await self._sibling_single_shot(
                base_url, model, headers, system_prompt, user_prompt,
                config_type, context, warnings,
            )
            if sib is not None:
                _log({"stage": "sibling-first", "ok": True, "warnings": warnings})
                return {
                    "generated_content": sib[0],
                    "filename_suggestion": filename,
                    "warnings": warnings,
                    "validation": sib[1],
                    "tool_rounds": tool_rounds,
                    "usage": dict(usage_total),
                }
            _log({"stage": "sibling-first", "ok": False, "warnings": warnings})

        # The env phase needs NO tools (its context already carries the var
        # manifest + trimmed env example). Disabling tools keeps the
        # conversation small: reasoning models otherwise burn their whole
        # token budget on chain-of-thought over tool results and return
        # EMPTY content (finish_reason=length).
        tools_enabled = config_type != "env_file"

        if progress:
            await progress(f"agent round 1/{TOOL_ROUNDS_MAX}")

        while tool_rounds < rounds_cap:
            tool_rounds += 1
            try:
                resp = await self._chat(
                    base_url, model, headers, messages,
                    tools=(TOOL_DEFINITIONS if tools_enabled else None),
                    on_delta=on_delta,
                )
            except Exception as exc:
                warnings.append(f"LLM call failed: {exc}")
                break
            if "_error" in resp:
                warnings.append(resp["_error"])
                break
            self._rec_usage_live(resp, usage_cb)
            choice = (resp.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            content = msg.get("content") or ""
            calls = msg.get("tool_calls") or []
            finish = choice.get("finish_reason")

            _log({
                "stage": "tool-loop",
                "round": tool_rounds,
                "finish_reason": finish,
                "content_head": _cap(content, 3000),
                "tool_calls": [c.get("function", {}).get("name") for c in calls],
                "reasoning": _cap(msg.get("reasoning_content") or ""),
            })

            # Early length detection (design §2 overflow avoidance): a
            # full-budget reasoning truncation means the reasoning model
            # failed — jump straight to the sibling instead of burning more
            # rounds on it.
            if finish == "length" and not content.strip():
                warnings.append(f"finish_reason=length at round {tool_rounds} — reasoning budget exhausted")
                early_length = True
                break

            if not calls:
                if tool_calls_seen == 0:
                    used_tools_ok = False  # model answered without any tool call
                break

            tool_calls_seen += 1
            # Execute every tool call in this round (bounded by caps).
            messages.append({"role": "assistant", "content": content or None, "tool_calls": calls})
            tool_logs: list[dict] = []
            for call in calls:
                fn = (call.get("function") or {})
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (ValueError, TypeError):
                    args = {}
                result = await self._exec_tool(name, args, context)
                tool_logs.append({
                    "name": name,
                    "args_head": _cap(json.dumps(args, ensure_ascii=False), 1000),
                    "result_head": _cap(result, 1500),
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id") or "",
                    "content": result,
                })
            _log({"stage": "tool-results", "round": tool_rounds, "tools": tool_logs})
            if progress:
                await progress(f"agent round {tool_rounds}/{rounds_cap} (tool: {name})")
        else:
            warnings.append(f"tool loop exhausted after {rounds_cap} rounds")

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
                resp = await self._chat(base_url, model, headers, messages, on_delta=on_delta)
            except Exception as exc:
                warnings.append(f"single-shot fallback failed: {exc}")
                content = ""
            else:
                self._rec_usage_live(resp, usage_cb)
                content = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                _log_resp("single-shot", resp, {"prompt_head": _cap(prefetched, 1500)})
                if "_error" in resp:
                    warnings.append(resp["_error"])
                    content = ""
            if not content:
                # One final content-only retry before giving up — the model
                # sometimes returns an empty first message (flaky) but
                # answers a direct "output now" instruction.
                content = await self._finalize_chat(base_url, model, headers, messages, warnings)
                if not content:
                    sib = await self._sibling_single_shot(
                        base_url, model, headers, system_prompt, user_prompt,
                        config_type, context, warnings,
                    )
                    if sib is not None:
                        return {
                            "generated_content": sib[0],
                            "filename_suggestion": filename,
                            "warnings": warnings,
                            "validation": sib[1],
                            "tool_rounds": tool_rounds,
                            "usage": dict(usage_total),
                        }
                    return {
                        "generated_content": "",
                        "filename_suggestion": filename,
                        "warnings": warnings + ["LLM returned no content"],
                        "validation": {"valid": False, "errors": ["LLM returned no content"]},
                        "tool_rounds": tool_rounds,
                        "usage": dict(usage_total),
                    }

        if not content.strip() and tool_calls_seen > 0 and not early_length:
            # Tool-loop exhausted with tool calls but no file content (the
            # model explored but never concluded) — force ONE content-only
            # call so the accumulated tool results become the answer.
            content = await self._finalize_chat(base_url, model, headers, messages, warnings)
            if not content:
                return {
                    "generated_content": "",
                    "filename_suggestion": filename,
                    "warnings": warnings + ["LLM returned no content after the tool loop"],
                    "validation": {"valid": False, "errors": ["LLM returned no content"]},
                    "tool_rounds": tool_rounds,
                    "usage": dict(usage_total),
                }

        extracted = self._extract_code_block(content, config_type)

        # ---- Mechanical validation + self-repair (bounded by the same cap) ----
        validation = await self._validate_generated(config_type, extracted, context)
        last_raw = content
        while not validation.get("valid") and tool_rounds < rounds_cap and validation.get("repairable", True):
            tool_rounds += 1
            messages.append({"role": "user", "content": self._build_repair_prompt(validation.get("errors", []))})
            if progress:
                await progress(f"self-repair round {tool_rounds}/{rounds_cap}")
            try:
                resp = await self._chat(base_url, model, headers, messages, on_delta=on_delta)
            except Exception as exc:
                warnings.append(f"self-repair call failed: {exc}")
                break
            if "_error" in resp:
                warnings.append(resp["_error"])
                break
            self._rec_usage_live(resp, usage_cb)
            _log_resp("repair", resp, {"round": tool_rounds})
            last_raw = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            extracted = self._extract_code_block(last_raw, config_type)
            if not extracted.strip():
                # Repair answer used tools / gave no file — finalize once.
                final = await self._finalize_chat(base_url, model, headers, messages, warnings)
                last_raw = final
                if final:
                    extracted = self._extract_code_block(final, config_type)
            validation = await self._validate_generated(config_type, extracted, context)

        # Stuck-in-tool-mode recovery: some reasoning models keep emitting
        # <invoke …> XML inside the content field (even with no tools in the
        # request) once the conversation contains tool messages. The XML is
        # stripped by the extractor, which then leaves EMPTY/invalid content
        # after every repair. Break the loop with a CLEAN single-shot call —
        # fresh system+user messages, no tools, no tool history.
        if (
            not validation.get("valid")
            and ("<invoke" in last_raw.lower() or "dsml" in last_raw.lower())
        ):
            warnings.append("tool-call XML contamination — retrying with a clean single-shot prompt")
            prefetched = await self._prefetch_prompt_urls(user_prompt)
            clean = []
            if system_prompt:
                clean.append({"role": "system", "content": system_prompt})
            clean.append({"role": "user", "content": prefetched})
            try:
                resp = await self._chat(base_url, model, headers, clean, on_delta=on_delta)
            except Exception as exc:
                warnings.append(f"clean single-shot failed: {exc}")
            else:
                if "_error" in resp:
                    warnings.append(resp["_error"])
                else:
                    clean_content = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                    clean_extracted = self._extract_code_block(clean_content, config_type)
                    clean_validation = await self._validate_generated(config_type, clean_extracted, context)
                    if clean_validation.get("valid"):
                        extracted = clean_extracted
                        validation = clean_validation

        # Non-thinking sibling fallback: deepseek reasoning variants can loop
        # endlessly in chain-of-thought (finish_reason=length, empty content)
        # on real-world prompts. Retry the clean single-shot with the
        # non-reasoning sibling model when the endpoint is a deepseek API.
        if not validation.get("valid"):
            sib = await self._sibling_single_shot(
                base_url, model, headers, system_prompt, user_prompt,
                config_type, context, warnings,
            )
            if sib is not None:
                extracted, validation = sib

        _log({"stage": "final", "valid": bool(validation.get("valid"))})
        return {
            "generated_content": extracted,
            "filename_suggestion": filename,
            "warnings": warnings,
            "validation": validation,
            "tool_rounds": tool_rounds,
            "usage": dict(usage_total),
        }

    async def _sibling_single_shot(
        self,
        base_url: str,
        model: str,
        headers: dict,
        system_prompt: str,
        user_prompt: str,
        config_type: str,
        context: dict,
        warnings: list[str],
    ) -> tuple[str, dict] | None:
        """Clean single-shot with the non-thinking sibling model.

        Returns ``(extracted_content, validation)`` when the sibling produced
        VALID output, else None.
        """
        sibling = self._non_thinking_sibling(base_url, model)
        if not sibling:
            return None
        warnings.append(f"retrying with non-thinking sibling model {sibling}")
        prefetched = await self._prefetch_prompt_urls(user_prompt)
        clean: list[dict] = []
        if system_prompt:
            clean.append({"role": "system", "content": system_prompt})
        clean.append({"role": "user", "content": prefetched})
        try:
            resp = await self._chat(base_url, sibling, headers, clean)
        except Exception as exc:
            warnings.append(f"sibling single-shot failed: {exc}")
            return None
        if "_error" in resp:
            warnings.append(resp["_error"])
            return None
        self._record_usage(resp)
        sib_content = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        sib_extracted = self._extract_code_block(sib_content, config_type)
        sib_validation = await self._validate_generated(config_type, sib_extracted, context)
        if sib_validation.get("valid"):
            return sib_extracted, sib_validation
        return None

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
        if config_type == "serving_ports":
            # No draft file: this validator reads the CONTENT and the compose in
            # play, so there is nothing to place on disk (unlike the compose and
            # nginx checks, whose tooling is path-based).
            from .config_validation import validate_serving_ports
            if not content.strip():
                return {"valid": False, "errors": ["empty serving_ports output"], "repairable": True}
            return validate_serving_ports(content, compose_paths)
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
        """Extract YAML/code blocks from LLM response.

        Also strips inline tool-call tags: reasoning models can emit
        ``<invoke name="...">…</invoke>`` XML INLINE in the content field
        instead of the tool_calls array — left in, it corrupts generated
        files (e.g. docker compose rejects ``.env`` lines starting with '<').
        """
        fence_lang = {
            "docker_compose": "yaml",
            "nginx_conf": "nginx",
            "env_file": "bash",
            "dockerfile": "dockerfile",
        }.get(config_type, "")

        pattern = rf"```(?:{fence_lang})?\s*\n(.*?)```"
        matches = re.findall(pattern, content, re.DOTALL)
        if matches:
            extracted = matches[0]
        else:
            # Some reasoning models fence code with CJK corner brackets:
            # <｜yaml> … <／yaml> or <｜ … ｜>.
            cjk = re.search(r"<｜[^｜]*｜>(.*?)<｜", content, re.DOTALL) or re.search(r"<｜(.*?)｜>", content, re.DOTALL)
            if cjk and cjk.group(1).strip():
                extracted = cjk.group(1)
            else:
                extracted = None
        if extracted is None:
            if config_type == "docker_compose":
                m = re.search(r"(version:|services:|name:)", content)
                extracted = content[m.start():] if m else content
            elif config_type == "nginx_conf":
                m = re.search(r"(server\s*\{|upstream\s+)", content)
                extracted = content[m.start():] if m else content
            elif config_type == "dockerfile":
                m = re.search(r"(FROM\s+\S+)", content, re.IGNORECASE)
                extracted = content[m.start():] if m else content
            else:
                extracted = content

        # DeepSeek can render its tool-call markup as SPECIAL-TOKEN TEXT
        # (｜DSML｜｜ invoke name="read_file"…) instead of the tool_calls array.
        # Any file built from that is garbage — emit nothing so the phase
        # retries (clean single-shot, no tools) instead of persisting junk.
        low = extracted.lower()
        if "dsml" in low or "｜invoke" in extracted or "|invoke" in low:
            return ""

        # Strip inline tool-call XML: reasoning models emit several variants —
        # <invoke name="…">…</invoke>, <invoke_tools>…</invoke_tools>, and
        # <parameter …> payload tags — all corrupt generated files.
        extracted = re.sub(
            r"</?(?:invoke|invoke_tools|parameter)\b[^>]*>.*?</(?:invoke|invoke_tools|parameter)>",
            "",
            extracted,
            flags=re.DOTALL,
        )
        extracted = re.sub(r"</?(?:invoke|invoke_tools|parameter)\b[^>]*/>?", "", extracted)
        # Stray CJK corner-bracket fence tokens (unmatched <｜ / ｜> pairs).
        extracted = extracted.replace("<｜", "").replace("｜>", "")
        return extracted.strip()


# Singleton
llm_service = LLMService()
