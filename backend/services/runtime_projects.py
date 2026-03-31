from pathlib import Path
import json

REGISTRY_PATH = Path("/data/runtime_projects.json")

DEFAULT_PROJECTS = {
    "acp-lab": {
        "project_key": "acp-lab",
        "display_name": "ACP-LAB",
        "icon": "🛣️",
        "runtime_kind": "legacy_bridge",
        "container_name": None,
        "host_workspace_path": "/data/workspaces/acp-lab/worktree",
        "container_workspace_path": "/data/workspaces/acp-lab/worktree",
        "enabled": True,
    },
    "hyperfocus": {
        "project_key": "hyperfocus",
        "display_name": "Hyperfocus",
        "icon": "⚡",
        "runtime_kind": "legacy_bridge",
        "container_name": None,
        "host_workspace_path": "/data/workspaces/hyperfocus",
        "container_workspace_path": "/data/workspaces/hyperfocus",
        "enabled": True,
    },
    "ego-metrology": {
        "project_key": "ego-metrology",
        "display_name": "EGO Metrology",
        "icon": "🧪",
        "runtime_kind": "legacy_bridge",
        "container_name": None,
        "host_workspace_path": "/data/workspaces/acp-lab/worktree/ego_metrology",
        "container_workspace_path": "/data/workspaces/acp-lab/worktree/ego_metrology",
        "enabled": True,
    },
}


def _normalize(projects):
    out = {}
    for key, value in (projects or {}).items():
        if not isinstance(value, dict):
            continue
        project_key = str(value.get("project_key") or key).strip().lower()
        if not project_key:
            continue
        item = dict(value)
        item["project_key"] = project_key
        item.setdefault("display_name", project_key)
        item.setdefault("icon", "📁")
        item.setdefault("runtime_kind", "legacy_bridge")
        item.setdefault("container_name", None)
        item.setdefault("host_workspace_path", f"/data/workspaces/{project_key}")
        item.setdefault("container_workspace_path", item["host_workspace_path"])
        item.setdefault("enabled", True)
        out[project_key] = item
    return out


def _load_registry():
    reg = {}
    if REGISTRY_PATH.exists():
        try:
            data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                reg = _normalize(data)
        except Exception:
            reg = {}
    merged = dict(_normalize(DEFAULT_PROJECTS))
    merged.update(reg)
    return merged


def _save_registry(projects):
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(
        json.dumps(projects, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def list_runtime_projects():
    return list(_load_registry().values())


def get_runtime_project(project_key):
    return _load_registry().get(str(project_key or "").strip().lower())


def resolve_runtime_project(project_key):
    project = get_runtime_project(project_key)
    if not project:
        raise KeyError(f"Unknown runtime project: {project_key}")
    return project


def resolve_host_workspace(project_key):
    pth = resolve_runtime_project(project_key).get("host_workspace_path")
    return Path(pth) if pth else None


def resolve_container_workspace(project_key):
    return resolve_runtime_project(project_key)["container_workspace_path"]


def create_runtime_project(project_key, display_name=None):
    key = str(project_key or "").strip().lower()
    if not key:
        raise ValueError("project_key is required")
    projects = _load_registry()
    if key in projects:
        raise ValueError(f"Project already exists: {key}")
    projects[key] = {
        "project_key": key,
        "display_name": display_name or key,
        "icon": "📁",
        "runtime_kind": "legacy_bridge",
        "container_name": None,
        "host_workspace_path": f"/data/workspaces/{key}",
        "container_workspace_path": f"/data/workspaces/{key}",
        "enabled": True,
    }
    _save_registry(projects)
    return projects[key]


def delete_runtime_project(project_key):
    key = str(project_key or "").strip().lower()
    if key in {"acp-lab", "hyperfocus", "ego-metrology"}:
        raise ValueError(f"{key} is a protected default project")
    projects = _load_registry()
    if key not in projects:
        raise ValueError(f"Unknown runtime project: {key}")
    removed = projects.pop(key)
    _save_registry(projects)
    return removed


if not REGISTRY_PATH.exists():
    _save_registry(_normalize(DEFAULT_PROJECTS))
