"""Write the generated HTML minisite to the local web root.

On the production server the factory runs as a systemd service with
write access to /var/www/armybots (Caddy's document root).  The
MINIAPP_WEB_ROOT env var overrides the path for local development.
"""

import os
from pathlib import Path
from typing import Optional

_WEB_ROOT = Path(os.getenv("MINIAPP_WEB_ROOT", "/var/www/armybots"))
_BASE_URL = os.getenv("MINIAPP_BASE_URL", "https://armybots.ru")


def _app_dir(bot_id: int) -> Path:
    d = _WEB_ROOT / "apps" / str(bot_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_miniapp_html(bot_id: int, html_content: str) -> str:
    """Write HTML to <web_root>/apps/<bot_id>/index.html and return the public URL."""
    (_app_dir(bot_id) / "index.html").write_text(html_content, encoding="utf-8")
    return f"{_BASE_URL}/apps/{bot_id}/"


def save_logo(bot_id: int, data: bytes, ext: str = "jpg") -> str:
    """Save logo image bytes and return the relative filename (e.g. 'logo.jpg').

    The HTML template references the logo by relative path so it works both
    on production (served by Caddy) and in local file:// previews.
    """
    filename = f"logo.{ext}"
    (_app_dir(bot_id) / filename).write_bytes(data)
    return filename
