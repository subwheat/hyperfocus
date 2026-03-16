"""
================================================================================
                    DOCUMENTS ROUTER — Create/Update Documents
================================================================================
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
import logging
from pathlib import Path

from ..services.artefacts import artefact_service
from ..services.document_service import document_service
from ..models import get_db
from ..auth import get_current_workspace

logger = logging.getLogger("DOCUMENTS_ROUTER")

router = APIRouter(prefix="/documents", tags=["documents"])


class CreateDocumentRequest(BaseModel):
    content: str
    filename: str
    format: str = "md"
    title: Optional[str] = None


class UpdateDocumentRequest(BaseModel):
    artefact_id: str
    new_content: str


class DocumentResponse(BaseModel):
    success: bool
    artefact_id: Optional[str] = None
    filename: Optional[str] = None
    message: str


@router.post("/create", response_model=DocumentResponse)
async def create_document(
    request: CreateDocumentRequest,
    db=Depends(get_db),
    auth=Depends(get_current_workspace)
):
    """Create a new document (MD, DOCX, or PDF)"""
    try:
        filename = request.filename
        if not any(filename.endswith(ext) for ext in ['.md', '.docx', '.pdf']):
            filename = f"{filename}.{request.format}"
        
        # For MD, use artefact_service directly
        if request.format == "md":
            artefact = await artefact_service.create_from_content(
                db=db,
                workspace_id=auth.workspace_id,
                filename=filename,
                content=request.content,
                title=request.title,
                source="chat"
            )
            return DocumentResponse(
                success=True,
                artefact_id=artefact.id,
                filename=filename,
                message=f"Document created: {filename}"
            )
        
        # For DOCX and PDF, create file then register
        if request.format == "docx":
            filepath = document_service.create_docx(request.content, filename, request.title)
        elif request.format == "pdf":
            filepath = document_service.create_pdf(request.content, filename, request.title)
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported format: {request.format}")
        
        # Read the created file and register as artefact
        file_content = filepath.read_text(errors='ignore') if request.format == 'md' else f"[Binary {request.format} file]"
        artefact = await artefact_service.create_from_content(
            db=db,
            workspace_id=auth.workspace_id,
            filename=filename,
            content=file_content,
            title=request.title,
            source="chat"
        )
        
        # Overwrite the artefact file with the proper binary
        import shutil
        artefact_path = Path(artefact.file_path)
        shutil.copy2(filepath, artefact_path)
        filepath.unlink()  # Remove temp file
        
        return DocumentResponse(
            success=True,
            artefact_id=artefact.id,
            filename=filename,
            message=f"Document created: {filename}"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Document creation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/update", response_model=DocumentResponse)
async def update_document(
    request: UpdateDocumentRequest,
    db=Depends(get_db),
    auth=Depends(get_current_workspace)
):
    """Update an existing document"""
    try:
        artefact = await artefact_service.get_by_id(db, request.artefact_id, auth.workspace_id)
        if not artefact:
            raise HTTPException(status_code=404, detail="Artefact not found")
        
        filepath = Path(artefact.file_path)
        ext = filepath.suffix.lower()
        
        if ext == '.md' or ext == '.txt':
            filepath.write_text(request.new_content, encoding='utf-8')
        elif ext == '.docx':
            document_service.create_docx(request.new_content, filepath.name)
            import shutil
            temp = document_service.artefacts_root / filepath.name
            shutil.copy2(temp, filepath)
            temp.unlink()
        elif ext == '.pdf':
            document_service.create_pdf(request.new_content, filepath.name)
            import shutil
            temp = document_service.artefacts_root / filepath.name
            shutil.copy2(temp, filepath)
            temp.unlink()
        else:
            raise HTTPException(status_code=400, detail=f"Cannot update {ext} files")
        
        return DocumentResponse(
            success=True,
            artefact_id=artefact.id,
            filename=artefact.filename,
            message=f"Document updated: {artefact.filename}"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Document update error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
