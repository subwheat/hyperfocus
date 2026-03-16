import asyncio
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from .artefacts import artefact_service
from ..models import Artefact

CACHE_DIR = Path("/data/artefacts_text_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

def _strip_html(html: str) -> str:
    # Low-cost, deterministic HTML -> text
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = re.sub(r"[ \t\r\f\v]+", " ", html)
    html = re.sub(r"\n\s*\n+", "\n\n", html)
    return html.strip()

def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[... tronqué ...]"

async def extract_text(artefact: Artefact, max_chars: int = 40000) -> Optional[str]:
    # Cache by sha256 if available
    key = (artefact.sha256 or artefact.id).strip()
    cache_path = CACHE_DIR / f"{key}.txt"
    if cache_path.exists():
        try:
            return _truncate(cache_path.read_text("utf-8"), max_chars)
        except Exception:
            pass

    file_path = await artefact_service.get_file_path(artefact)
    if not file_path.exists():
        return None

    mt = (artefact.mime_type or "").lower()
    ft = (artefact.file_type or "").lower()

    text: Optional[str] = None

    # Text-like (already supported)
    if mt.startswith("text/") or ft in ("md","json","csv","txt","py","js","html","css","yaml","yml","toml","xml"):
        text = await artefact_service.read_content(artefact)
        if text and (mt == "text/html" or ft == "html"):
            text = _strip_html(text)

    # PDF via pdftotext (gold/low cost)
    elif mt == "application/pdf" or ft == "pdf":
        def run_pdftotext() -> str:
            # stdout mode: last arg "-" => output to stdout
            cp = subprocess.run(
                ["pdftotext", "-layout", str(file_path), "-"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            out = (cp.stdout or b"").decode("utf-8", errors="replace").strip()
            return out

        text = await asyncio.to_thread(run_pdftotext)

    # Otherwise (png, etc.): no OCR by default (cost)
    else:
        text = None

    if not text:
        return None

    # Write cache
    try:
        cache_path.write_text(text, "utf-8")
    except Exception:
        pass

    return _truncate(text, max_chars)
