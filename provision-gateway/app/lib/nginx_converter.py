"""Lightweight nginx-to-Jinja2 converter for provision-gateway."""

from __future__ import annotations

import re
from pathlib import Path


def nginx_file_to_template(
    src_path: str,
    dst_path: str,
    service_name_hint: str = "",
    compose_service_names: list[str] | None = None,
) -> str:
    """Convert a plain nginx conf to a Jinja2 .j2 template."""
    text = Path(src_path).read_text()
    
    # server_name → {{ hostname }}
    text = re.sub(
        r'([ \t]*server_name[ \t]+)[^\n;]+(;)',
        r'\1{{ hostname }}\2',
        text,
    )
    
    # auth_basic string
    text = re.sub(
        r'([ \t]*auth_basic[ \t]+)"[^"]*"(;)',
        r'\1"{{ service_name }} - {{ user_name }}"\2',
        text,
    )
    
    # auth_basic_user_file path
    text = re.sub(
        r'([ \t]*auth_basic_user_file[ \t]+)\S+(;)',
        r'\1{{ htpasswd_path }}\2',
        text,
    )
    
    # ssl_certificate path
    text = re.sub(
        r'([ \t]*ssl_certificate[ \t]+)\S+(;)',
        r'\1{{ ssl_certificate_path }}\2',
        text,
    )
    
    # ssl_certificate_key path
    text = re.sub(
        r'([ \t]*ssl_certificate_key[ \t]+)\S+(;)',
        r'\1{{ ssl_certificate_key_path }}\2',
        text,
    )
    
    # proxy_pass target rewriting
    if compose_service_names:
        for name in compose_service_names:
            text = re.sub(
                rf'(proxy_pass\s+https?://){re.escape(name)}(:\d+)?',
                rf'\1{{{{ container_prefix }}}}{name}\2',
                text,
            )
    elif service_name_hint:
        # Rewrite proxy_pass targets that start with the service name hint
        text = re.sub(
            rf'(proxy_pass\s+https?://){re.escape(service_name_hint)}-(\S+)',
            rf'\1{{{{ container_prefix }}}}\2',
            text,
        )
    
    # Wrap HTTPS blocks
    if re.search(r'listen\s+[^;]*\bssl\b', text):
        text = _wrap_https_blocks(text)
    
    # Build header
    stem = Path(src_path).stem
    hint = service_name_hint or "myapp"
    header = f"""# {stem}.nginx.conf.j2 — generated template
#
# Template variables:
#   {{{{ hostname }}}}          e.g. {hint}-alice-0.example.com
#   {{{{ container_prefix }}}}  e.g. {hint}-user_alice-0-
#   {{{{ htpasswd_path }}}}     absolute path to .htpasswd file
#   {{{{ user_name }}}}
#   {{{{ service_name }}}}
#   {{{{ label }}}}
#   {{{{ domain_name }}}}
#   {{{{ https }}}}
#   {{{{ ssl_certificate_path }}}}
#   {{{{ ssl_certificate_key_path }}}}
#
"""
    
    Path(dst_path).write_text(header + text)
    return str(dst_path)


def _wrap_https_blocks(text: str) -> str:
    """Wrap SSL server blocks in {% if https %}...{% endif %}."""
    lines = text.split("\n")
    result = []
    in_ssl_block = False
    block_started = False
    
    for line in lines:
        if re.match(r'^\s*server\s*\{', line):
            result.append(line)
            block_started = True
            in_ssl_block = False
        elif block_started and re.search(r'listen\s+[^;]*\bssl\b', line):
            # This is an SSL block — wrap it
            in_ssl_block = True
            # Insert {% if https %} before the server block
            # Find the last server { line and prepend
            for i in range(len(result) - 1, -1, -1):
                if re.match(r'^\s*server\s*\{', result[i]):
                    result.insert(i, "\n{% if https %}")
                    break
            result.append(line)
        elif block_started and line.strip() == "}" and in_ssl_block:
            result.append(line)
            result.append("{% endif %}\n")
            block_started = False
            in_ssl_block = False
        elif block_started and line.strip() == "}":
            result.append(line)
            block_started = False
        else:
            result.append(line)
    
    return "\n".join(result)
