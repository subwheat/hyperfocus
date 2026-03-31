"""
Terminal Bridge V2 - Full Access Sandboxes
==========================================
Permet au LLM d'exécuter des commandes dans les sandboxes Philae.
"""

import asyncio
import re
import time
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
import subprocess
from .runtime_projects import list_runtime_projects, get_runtime_project, create_runtime_project, delete_runtime_project

router = APIRouter(prefix="/api/sandbox", tags=["Sandbox Terminal"])

# ============================================================================
# CONFIGURATION
# ============================================================================

SANDBOX_PROJECTS = {
    "sentinel": {
        "container": "sandbox-sentinel",
        "port": 9004,
        "description": "Lane A — ACP core",
        "icon": "🛣️"
    },
    "hyperfocus": {
        "container": "sandbox-hyperfocus",
        "port": 9007,
        "description": "Hyperfocus — self-modification",
        "icon": "⚡"
    },
}

# Commandes VRAIMENT dangereuses (même en sandbox)
HARD_BLOCKED_PATTERNS = [
    r"rm\s+-rf\s+/\s*$",           # rm -rf / (root only)
    r"rm\s+-rf\s+/\*",             # rm -rf /*
    r"mkfs\.",                      # Format disk
    r"dd\s+if=.*/dev/sd",          # Write to disk
    r">\s*/dev/sd",                # Redirect to disk
    r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;", # Fork bomb
]

# ============================================================================
# SCHEMAS
# ============================================================================

class ExecuteRequest(BaseModel):
    command: str
    working_dir: Optional[str] = "/workspace"
    timeout: int = 120
    
class ExecuteResponse(BaseModel):
    success: bool
    output: str
    stderr: str
    return_code: int
    execution_time: float
    project: str
    command: str

class ProjectStatus(BaseModel):
    name: str
    container: str
    status: str
    icon: str
    description: str
    port: int

class CreateProjectRequest(BaseModel):
    project_key: str
    display_name: Optional[str] = None

# ============================================================================
# HELPERS
# ============================================================================

def check_container_running(container_name: str) -> bool:
    """Vérifie si un container est en cours d'exécution"""
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() == "true"
    except Exception:
        return False

def is_blocked_command(command: str) -> Optional[str]:
    """Vérifie si la commande est bloquée"""
    for pattern in HARD_BLOCKED_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return pattern
    return None

async def execute_in_container(container: str, command: str, working_dir: str, timeout: int) -> dict:
    """Exécute une commande dans un container Docker"""
    
    # Sanitize command before execution
    # Fix: "& &&" is invalid bash (background + AND)
    command = command.replace('& &&', '&&')
    command = command.replace('& ;', ';')
    # Strip trailing & (background) to avoid zombie processes
    command = re.sub(r'&\s*$', '', command.strip())
    # Remove empty commands from chaining
    command = re.sub(r'&&\s*&&', '&&', command)
    command = command.strip()
    
    if not command:
        return {"success": False, "output": "", "stderr": "Empty command", "exit_code": 1, "duration": 0}
    
    # Construire la commande docker exec
    full_command = f'cd {working_dir} && {command}'
    docker_cmd = ["docker", "exec", container, "bash", "-c", full_command]
    
    start_time = time.time()
    
    try:
        # Exécution async
        process = await asyncio.create_subprocess_exec(
            *docker_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            process.kill()
            return {
                "success": False,
                "output": "",
                "stderr": f"Command timed out after {timeout}s",
                "return_code": -1,
                "execution_time": timeout
            }
        
        return {
            "success": process.returncode == 0,
            "output": stdout.decode('utf-8', errors='replace')[:100000],  # 100KB max
            "stderr": stderr.decode('utf-8', errors='replace')[:20000],
            "return_code": process.returncode,
            "execution_time": round(time.time() - start_time, 3)
        }
        
    except Exception as e:
        return {
            "success": False,
            "output": "",
            "stderr": str(e),
            "return_code": -1,
            "execution_time": round(time.time() - start_time, 3)
        }

# ============================================================================
# ROUTES
# ============================================================================

@router.get("/projects")
async def list_projects():
    """Liste tous les projets runtime disponibles"""
    projects = []

    for project in list_runtime_projects():
        name = project["project_key"]
        runtime_kind = project.get("runtime_kind", "legacy_bridge")
        container = project.get("container_name") or ""
        if runtime_kind == "docker_sandbox" and container:
            status = "running" if check_container_running(container) else "stopped"
        else:
            status = "legacy"
        projects.append({
            "name": name,
            "container": container,
            "status": status,
            "icon": project.get("icon", "📁"),
            "description": project.get("display_name", name),
            "port": 0,
            "runtime_kind": runtime_kind
        })

    return {"projects": projects}


@router.post("/projects")
async def create_project(request: CreateProjectRequest):
    """Créer un projet runtime persistant"""
    try:
        project = create_runtime_project(request.project_key, request.display_name)
    except ValueError as e:
        raise HTTPException(400, str(e))

    runtime_kind = project.get("runtime_kind", "legacy_bridge")
    return {
        "project": {
            "name": project["project_key"],
            "container": project.get("container_name") or "",
            "status": "legacy" if runtime_kind != "docker_sandbox" else "stopped",
            "icon": project.get("icon", "📁"),
            "description": project.get("display_name", project["project_key"]),
            "port": 0,
            "runtime_kind": runtime_kind,
        }
    }


@router.delete("/projects/{project}")
async def delete_project(project: str):
    """Supprimer un projet runtime persistant"""
    try:
        removed = delete_runtime_project(project)
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {
        "deleted": removed["project_key"],
        "ok": True,
    }


@router.get("/projects/{project}")
async def get_project_status(project: str):
    """Statut détaillé d'un projet"""
    
    runtime_project = get_runtime_project(project)
    if not runtime_project:
        raise HTTPException(404, f"Project '{project}' not found")

    runtime_kind = runtime_project.get("runtime_kind", "legacy_bridge")
    config = {
        "container": runtime_project.get("container_name") or "",
        "icon": runtime_project.get("icon", "📁"),
        "description": runtime_project.get("display_name", project),
        "port": 0,
    }
    container = config["container"]

    if runtime_kind != "docker_sandbox":
        return {
            "project": project,
            "status": "legacy",
            "container": container,
            "runtime_kind": runtime_kind,
            "message": f"Project '{project}' is configured as legacy_bridge on this machine"
        }

    # Vérifier le statut
    is_running = check_container_running(container)
    
    if not is_running:
        return {
            "project": project,
            "status": "stopped",
            "container": container,
            "runtime_kind": runtime_kind,
            "message": f"Container {container} is not running"
        }
    
    # Récupérer des infos sur le container
    try:
        result = subprocess.run(
            ["docker", "exec", container, "df", "-h", "/workspace"],
            capture_output=True, text=True, timeout=5
        )
        disk_info = result.stdout
    except:
        disk_info = "N/A"
    
    return {
        "project": project,
        "status": "running",
        "container": container,
        "icon": config["icon"],
        "description": config["description"],
        "port": config["port"],
        "runtime_kind": runtime_kind,
        "disk": disk_info
    }


@router.post("/projects/{project}/execute", response_model=ExecuteResponse)
async def execute_command(project: str, request: ExecuteRequest):
    """Exécuter une commande dans la sandbox du projet"""
    
    # Vérifier le projet
    runtime_project = get_runtime_project(project)
    if not runtime_project:
        raise HTTPException(404, f"Project '{project}' not found")

    runtime_kind = runtime_project.get("runtime_kind", "legacy_bridge")
    container = runtime_project.get("container_name") or ""

    if runtime_kind != "docker_sandbox":
        raise HTTPException(409, f"Project '{project}' is configured as legacy_bridge on this machine")

    # Vérifier que le container tourne
    if not check_container_running(container):
        raise HTTPException(503, f"Sandbox '{project}' is not running. Start it first.")
    
    # Vérifier commandes bloquées
    blocked = is_blocked_command(request.command)
    if blocked:
        raise HTTPException(403, f"Command blocked (pattern: {blocked})")
    
    # Exécuter
    result = await execute_in_container(
        container=container,
        command=request.command,
        working_dir=request.working_dir,
        timeout=request.timeout
    )
    
    return ExecuteResponse(
        success=result["success"],
        output=result["output"],
        stderr=result["stderr"],
        return_code=result["return_code"],
        execution_time=result["execution_time"],
        project=project,
        command=request.command
    )


@router.post("/projects/{project}/start")
async def start_project(project: str):
    """Démarrer la sandbox d'un projet"""
    
    runtime_project = get_runtime_project(project)
    if not runtime_project:
        raise HTTPException(404, f"Project '{project}' not found")

    runtime_kind = runtime_project.get("runtime_kind", "legacy_bridge")
    if runtime_kind != "docker_sandbox":
        raise HTTPException(409, f"Project '{project}' is configured as legacy_bridge on this machine")

    raise HTTPException(501, f"docker_sandbox start is not wired to the runtime registry yet for project '{project}'")


@router.post("/projects/{project}/stop")
async def stop_project(project: str):
    """Arrêter la sandbox d'un projet"""
    
    runtime_project = get_runtime_project(project)
    if not runtime_project:
        raise HTTPException(404, f"Project '{project}' not found")

    runtime_kind = runtime_project.get("runtime_kind", "legacy_bridge")
    if runtime_kind != "docker_sandbox":
        raise HTTPException(409, f"Project '{project}' is configured as legacy_bridge on this machine")

    raise HTTPException(501, f"docker_sandbox stop is not wired to the runtime registry yet for project '{project}'")


@router.post("/projects/{project}/install")
async def install_package(project: str, package: str, manager: str = "pip"):
    """Installer un package dans la sandbox"""
    
    if not get_runtime_project(project):
        raise HTTPException(404, f"Project '{project}' not found")
    
    if manager == "pip":
        command = f"pip install {package}"
    elif manager == "npm":
        command = f"npm install {package}"
    else:
        raise HTTPException(400, f"Unknown package manager: {manager}")
    
    return await execute_command(project, ExecuteRequest(
        command=command,
        working_dir="/workspace",
        timeout=300  # 5 min pour les installs
    ))
