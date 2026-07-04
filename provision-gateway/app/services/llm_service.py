"""LLM service — OpenAI-compatible client for config generation."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from ..models.llm_config import LLMConfig
from ..utils.crypto import decrypt_api_key, encrypt_api_key
from ..utils.file_scanner import RepoContext


class LLMService:
    """Manages LLM configuration and generates service config files."""

    # ------------------------------------------------------------------
    # Config management
    # ------------------------------------------------------------------

    def get_config(self, db: Session) -> dict:
        """Get the current LLM configuration."""
        config = db.query(LLMConfig).filter(LLMConfig.is_active == True).first()
        if not config:
            return {
                "mode": "local_agent",
                "agent_url": None,
                "agent_model": None,
                "byok_configured": False,
                "byok_model": None,
                "byok_api_key_masked": None,
                "is_active": False,
                "system_prompt": None,
            }
        return config.to_dict()

    def save_config(self, db: Session, data: dict) -> LLMConfig:
        """Save or update LLM configuration."""
        config = db.query(LLMConfig).filter(LLMConfig.is_active == True).first()
        
        if not config:
            config = LLMConfig()
            db.add(config)
        
        config.mode = data.get("mode", config.mode or "local_agent")
        config.agent_url = data.get("agent_url", config.agent_url)
        config.agent_model = data.get("agent_model", config.agent_model)
        config.byok_base_url = data.get("byok_base_url", config.byok_base_url)
        config.byok_model = data.get("byok_model", config.byok_model)
        config.system_prompt = data.get("system_prompt", config.system_prompt)
        config.is_active = True
        
        # Encrypt API key if provided (only if non-empty)
        if data.get("byok_api_key"):
            config.byok_api_key_enc = encrypt_api_key(data["byok_api_key"])
        elif "byok_api_key" in data and data["byok_api_key"] == "":
            # Empty string = keep existing key
            pass
        
        db.commit()
        db.refresh(config)
        return config

    # ------------------------------------------------------------------
    # LLM calls
    # ------------------------------------------------------------------

    def _resolve_endpoint(self, db: Session) -> tuple[str, str, dict]:
        """Resolve which LLM endpoint to use.
        
        Returns (base_url, model, extra_headers).
        """
        config = db.query(LLMConfig).filter(LLMConfig.is_active == True).first()
        
        if config and config.mode == "byok" and config.byok_api_key_enc:
            api_key = decrypt_api_key(config.byok_api_key_enc)
            base = config.byok_base_url or "https://api.openai.com/v1"
            model = config.byok_model or "gpt-4o"
            headers = {"Authorization": f"Bearer {api_key}"}
        elif config and config.agent_url:
            base = config.agent_url
            model = config.agent_model or "llama3.1:8b"
            headers = {}
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

    async def generate_config(
        self, db: Session, config_type: str, context: dict
    ) -> dict:
        """Generate a config file using LLM.
        
        config_type: 'docker_compose' | 'nginx_conf' | 'env_file' | 'dockerfile'
        context: RepoContext-like dict with repo info
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
            
            # Extract YAML/JSON block from response
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
        """Build a prompt for config generation."""
        desc = context.get("repo_description", "an application")
        files = context.get("repo_files", [])
        port = context.get("port", 8000)
        lang = context.get("language", "unknown")
        framework = context.get("framework", "unknown")
        
        if config_type == "docker_compose":
            return f"""Generate a docker-compose.yml for {desc}

Context:
- Language: {lang}
- Framework: {framework}
- Port: {port}
- Files in repo: {', '.join(files[:20])}
- Needs database: {context.get('needs_db', False)}
- Needs cache: {context.get('needs_cache', False)}

Requirements:
- Use version '3.8'
- Define one service named 'web' that builds from the current directory
- Set container_name to 'myapp-web' (will be templated later)
- Use a per-service network named 'app_network' (will be templated to {{{{ network_name }}}})
- Do NOT include 'ports' mapping (reverse proxy handles routing)
- Include a healthcheck if possible
- Include named volumes for persistent data

Output ONLY the raw YAML, no markdown fences, no explanations."""
        
        elif config_type == "nginx_conf":
            return f"""Generate an nginx reverse proxy configuration for {desc}

Context:
- App port: {port}
- Service will be behind provision-nginx
- Need basic auth support

Requirements:
- server_name will be templated to {{{{ hostname }}}}
- proxy_pass to http://CONTAINER_PREFIX:PORT (will be templated)
- Include auth_basic and auth_basic_user_file directives
- Include proxy headers (Host, X-Real-IP, X-Forwarded-For, X-Forwarded-Proto)
- WebSocket support (Upgrade, Connection headers)
- client_max_body_size 100m

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
        # Try fenced code blocks first
        fence_lang = {
            "docker_compose": "yaml",
            "nginx_conf": "nginx",
            "env_file": "bash",
            "dockerfile": "dockerfile",
        }.get(config_type, "")
        
        # Look for ```yaml ... ``` or ``` ... ```
        pattern = rf"```(?:{fence_lang})?\s*\n(.*?)```"
        matches = re.findall(pattern, content, re.DOTALL)
        if matches:
            return matches[0].strip()
        
        # If no fence found, return raw content (strip common prefix text)
        # Try to find where the actual config starts
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
