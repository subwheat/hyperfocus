"""
/api/me endpoint - returns current user info or 401
"""
from fastapi import APIRouter, Request, HTTPException

router = APIRouter()

@router.get("/me")
async def get_me(request: Request):
    """Return current user from X-Auth-User header (set by Caddy forward_auth)"""
    user = request.headers.get("X-Auth-User")
    role = request.headers.get("X-Auth-Role", "guest")
    
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    return {"user": user, "role": role}
