"""
ACP Project Memory V1
=====================

Mémoire persistante backend, reconstruite depuis les artefacts du workspace.
V1 déterministe : pas d'appel LLM pendant l'upload.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Artefact
from .artefact_extract import extract_text

PROJECT_MEMORY_ROOT = Path(os.getenv("PROJECT_MEMORY_ROOT", "/data/project_memory"))
PROJECT_MEMORY_ROOT.mkdir(parents=True, exist_ok=True)

PROJECT_RE = re.compile(r"(?mi)^\s*Project:\s*([a-z0-9_-]{1,64})\s*$")
DIGEST_RE = re.compile(r"^<!-- ACP_MEMORY_DIGEST:([a-f0-9]{64}) -->\n?", re.M)


def sanitize_project_key(value: str | None) -> str:
    value = str(value or "acp").strip().lower()
    value = re.sub(r"[^a-z0-9_-]+", "_", value)
    return value[:64] or "acp"


def _msg_content(msg: Any) -> str:
    if isinstance(msg, dict):
        return str(msg.get("content") or "")
    return str(getattr(msg, "content", "") or "")


def detect_project_key_from_messages(messages: Iterable[Any]) -> str:
    for msg in messages or []:
        content = _msg_content(msg)
        if not content:
            continue
        m = PROJECT_RE.search(content)
        if m:
            return sanitize_project_key(m.group(1))
    return "acp"


def _memory_dir(workspace_id: str) -> Path:
    p = PROJECT_MEMORY_ROOT / str(workspace_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _memory_path(workspace_id: str, project_key: str) -> Path:
    return _memory_dir(workspace_id) / f"ACP_CONTEXT_{sanitize_project_key(project_key)}.md"


def _existing_digest(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    m = DIGEST_RE.match(raw)
    return m.group(1) if m else None


def _strip_digest_header(raw: str) -> str:
    return DIGEST_RE.sub("", raw, count=1)


def load_project_memory_text(
    workspace_id: str,
    project_key: str,
    max_chars: int = 12000,
) -> str:
    path = _memory_path(workspace_id, project_key)
    if not path.exists():
        return ""
    raw = path.read_text(encoding="utf-8", errors="ignore")
    text = _strip_digest_header(raw)
    return text[:max_chars]


def _build_digest(artefacts: list[Artefact]) -> str:
    rows = []
    for a in artefacts:
        rows.append(
            "|".join(
                [
                    str(getattr(a, "id", "") or ""),
                    str(getattr(a, "filename", "") or ""),
                    str(getattr(a, "file_size", 0) or 0),
                    getattr(a, "created_at", None).isoformat()
                    if getattr(a, "created_at", None)
                    else "",
                    str(getattr(a, "source", "") or ""),
                ]
            )
        )
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def _compact_excerpt(text: str, max_chars: int = 1600) -> str:
    text = (text or "").replace("\r", "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""

    preferred: list[str] = []
    for ln in lines:
        if (
            ln.startswith(("#", "-", "*", "•"))
            or re.match(r"^[A-Z0-9][A-Za-z0-9 _/\-]{1,80}:$", ln)
        ):
            preferred.append(ln)
        if len("\n".join(preferred)) >= max_chars // 2:
            break

    flat = " ".join(lines)
    excerpt = "\n".join(preferred).strip()

    if len(excerpt) < max_chars // 3:
        excerpt = flat[:max_chars]
    elif len(excerpt) < max_chars:
        remaining = max_chars - len(excerpt) - 2
        if remaining > 80:
            excerpt = (excerpt + "\n\n" + flat[:remaining]).strip()

    return excerpt[:max_chars].strip()


async def ensure_project_memory_current(
    db: AsyncSession,
    workspace_id: str,
    project_key: str,
    artefact_limit: int = 25,
) -> str:
    project_key = sanitize_project_key(project_key)
    path = _memory_path(workspace_id, project_key)

    result = await db.execute(
        select(Artefact)
        .where(Artefact.workspace_id == workspace_id)
        .where(Artefact.project_key == project_key)
        .order_by(Artefact.created_at.desc())
    )
    all_artefacts = list(result.scalars().all())

    artefacts: list[Artefact] = []
    for a in all_artefacts:
        filename = str(getattr(a, "filename", "") or "")
        if not filename:
            continue
        if filename.startswith("ACP_CONTEXT_"):
            continue
        artefacts.append(a)
        if len(artefacts) >= artefact_limit:
            break

    digest = _build_digest(artefacts)
    if path.exists() and _existing_digest(path) == digest:
        return load_project_memory_text(workspace_id, project_key)

    sections: list[str] = []
    for a in artefacts:
        try:
            extracted = await extract_text(a, max_chars=4000)
        except Exception:
            extracted = ""

        excerpt = _compact_excerpt(extracted or "", max_chars=1600)
        if not excerpt:
            continue

        created = getattr(a, "created_at", None)
        created_str = created.isoformat() if created else ""

        sections.append(
            "\n".join(
                [
                    f"## {getattr(a, 'filename', 'artefact')}",
                    "",
                    f"- Artefact ID: {getattr(a, 'id', '')}",
                    f"- Source: {getattr(a, 'source', '') or ''}",
                    f"- Type: {getattr(a, 'file_type', '') or getattr(a, 'mime_type', '') or ''}",
                    f"- Taille: {getattr(a, 'file_size', 0) or 0}",
                    f"- Date: {created_str}",
                    "",
                    excerpt,
                    "",
                ]
            )
        )

    header = "\n".join(
        [
            "# ACP_CONTEXT",
            "",
            f"Projet: {project_key}",
            f"Workspace: {workspace_id}",
            f"Mise à jour: {datetime.now(timezone.utc).isoformat()}",
            "",
            "Mémoire persistante backend du projet.",
            "Source de travail synthétique construite automatiquement depuis les artefacts du workspace.",
            "",
        ]
    )

    if sections:
        body = header + "\n---\n\n".join(sections)
    else:
        body = header + "Aucun artefact textuel exploitable pour le moment.\n"

    path.write_text(
        f"<!-- ACP_MEMORY_DIGEST:{digest} -->\n{body}",
        encoding="utf-8",
    )

    return load_project_memory_text(workspace_id, project_key)


def prepend_project_memory_to_messages(
    messages: list[Any],
    memory_text: str,
    project_key: str,
) -> list[Any]:
    if not memory_text:
        return messages

    prefix = (
        f"[CONTEXTE PROJET SERVEUR: {sanitize_project_key(project_key)}]\n"
        "Le contexte ci-dessous est injecté maintenant par le serveur dans cette requête.\n"
        "Il est disponible immédiatement et doit être utilisé comme source par défaut du projet.\n"
        "Si des artefacts bruts sont joints explicitement, ils restent prioritaires.\n"
        "N'affirme jamais que tu n'as pas accès à ce contexte si ce bloc est présent.\n"
        "Réponds directement à partir de ce contexte.\n\n"
        f"{memory_text.strip()}"
    )

    for msg in messages:
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
        if role != "user":
            continue

        content = _msg_content(msg)
        if prefix in content:
            return messages

        new_content = prefix + "\n\n" + content
        if isinstance(msg, dict):
            msg["content"] = new_content
        else:
            setattr(msg, "content", new_content)
        return messages

    return messages
