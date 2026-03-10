"""FastAPI dependency injection — DB session, auth, Redis."""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db.session import get_db


async def verify_api_key(
    x_api_key: str = Header(alias="X-API-Key", default=""),
) -> str:
    if x_api_key != settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
    return x_api_key


# Shorthand dependencies
DBSession = Depends(get_db)
APIKeyAuth = Depends(verify_api_key)
