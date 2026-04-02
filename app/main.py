"""
Микросервис управления профилями разработчиков.
"""

import os
from typing import Optional

from app import models, schemas
from app.database import get_db

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from jose import ExpiredSignatureError, JWTError, jwt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


app = FastAPI(
    title="Profile Service API",
    description="Сервис для управления профилями разработчиков",
    version="1.0.0",
)

INTEGRATIONS_BASE_URL = os.getenv("INTEGRATIONS_BASE_URL", "http://integrations:8004")
AUTH_SERVICE_URL = os.getenv("AUTH_SERVICE_URL", "http://auth-service:8000")
JWT_SECRET = os.getenv("JWT_SECRET", "change-me-in-env")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
PUBLIC_PATHS = {"/", "/docs", "/openapi.json", "/redoc"}


def _extract_user_id(payload: dict) -> Optional[int]:
    user_id = payload.get("user_id") or payload.get("id") or payload.get("sub")
    try:
        return int(user_id) if user_id is not None else None
    except (TypeError, ValueError):
        return None


@app.middleware("http")
async def jwt_auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS:
        return await call_next(request)

    authorization = request.headers.get("Authorization")
    if not authorization or not authorization.lower().startswith("bearer "):
        return JSONResponse(status_code=401, content={"detail": "Missing token"})

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        return JSONResponse(status_code=401, content={"detail": "Missing token"})

    try:
        payload = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            options={"verify_aud": False},
        )
    except ExpiredSignatureError:
        return RedirectResponse(url=f"{AUTH_SERVICE_URL}/auth/login", status_code=307)
    except JWTError:
        return JSONResponse(status_code=401, content={"detail": "Invalid token"})

    user_id = _extract_user_id(payload)
    if user_id is None:
        return JSONResponse(status_code=401, content={"detail": "Invalid token payload"})

    request.state.user_id = user_id
    return await call_next(request)


async def get_current_user_id(request: Request) -> int:
    user_id = getattr(request.state, "user_id", None)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user_id


# ----- Корневой эндпоинт -----


@app.get("/")
def root():
    return {
        "message": "Profile Service API",
        "docs": "/docs",
        "status": "running with PostgreSQL",
    }


# ----- POST /profile — создать профиль -----


@app.post("/profile", response_model=schemas.ProfileResponse, status_code=201)
async def create_profile(
    profile_in: schemas.ProfileCreate,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    db_profile = models.Profile(
        user_id=user_id,
        username=profile_in.username,
        bio=profile_in.bio,
        stack=profile_in.stack,
        experience=profile_in.experience,
        skills=profile_in.skills,
        interests=profile_in.interests,
        short_term_goals=profile_in.short_term_goals,
        long_term_goals=profile_in.long_term_goals,
    )
    db.add(db_profile)
    await db.commit()
    await db.refresh(db_profile)
    return db_profile


@app.post("/profile/integrations/monkeytype/fetch")
async def fetch_monkeytype_via_integrations(
    body: schemas.MonkeytypeProxyRequest,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    """
    Прокси-ручка profile-service:
    - принимает username
    - добавляет user_id текущего пользователя
    - дергает integrations-service по HTTP
    - возвращает ответ integrations-service как есть
    """
    url = f"{INTEGRATIONS_BASE_URL}/integrations/monkeytype/fetch"
    stmt = select(models.Profile).where(models.Profile.user_id == user_id)
    result = await db.execute(stmt)
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    payload = {
        "profile_id": profile.id,
        "username": body.username,
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(url, json=payload)
    except httpx.HTTPError:
        raise HTTPException(
            status_code=503,
            detail="Integrations service is unavailable",
        )

    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        return JSONResponse(status_code=response.status_code, content=response.json())
    return JSONResponse(
        status_code=response.status_code,
        content={"detail": response.text},
    )


# ----- GET /profile/me — профиль текущего пользователя -----


@app.get("/profile/me", response_model=schemas.ProfileResponse)
async def read_profile_me(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    stmt = select(models.Profile).where(models.Profile.user_id == user_id)
    result = await db.execute(stmt)
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


# ----- PATCH /profile/me — обновить свой профиль -----


@app.patch("/profile/me", response_model=schemas.ProfileResponse) 
async def update_profile_me(
    profile_update: schemas.ProfileUpdate,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    stmt = select(models.Profile).where(models.Profile.user_id == user_id)
    result = await db.execute(stmt)
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    update_data = profile_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(profile, field, value)

    await db.commit()
    await db.refresh(profile)
    return profile


# ----- GET /profile/by-username/{username} — публичный профиль -----


@app.get(
    "/profile/by-username/{username}",
    response_model=schemas.ProfileResponse,
)
async def read_profile_by_username(
    username: str,
    db: AsyncSession = Depends(get_db),
    _: int = Depends(get_current_user_id),
):
    stmt = select(models.Profile).where(models.Profile.username == username)
    result = await db.execute(stmt)
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


# ----- GET /profile/{profile_id} — профиль по ID -----


@app.get("/profile/{profile_id}", response_model=schemas.ProfileResponse)
async def read_profile_by_id(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    _: int = Depends(get_current_user_id),
):
    profile = await db.get(models.Profile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


# ----- PATCH /profile/{profile_id} — обновить по ID (админ/внутренние сервисы) -----


@app.patch("/profile/{profile_id}", response_model=schemas.ProfileResponse)
async def update_profile_by_id(
    profile_id: int,
    profile_update: schemas.ProfileUpdate,
    db: AsyncSession = Depends(get_db),
    _: int = Depends(get_current_user_id),
):
    profile = await db.get(models.Profile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    update_data = profile_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(profile, field, value)

    await db.commit()
    await db.refresh(profile)
    return profile


# ----- DELETE /profile/me — удалить свой профиль -----


@app.delete("/profile/me", status_code=204)
async def delete_profile_me(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    stmt = select(models.Profile).where(models.Profile.user_id == user_id)
    result = await db.execute(stmt)
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    await db.delete(profile)
    await db.commit()
    return


# ----- DELETE /profile/{profile_id} — удалить по ID (админ/внутренние сервисы) -----


@app.delete("/profile/{profile_id}", status_code=204)
async def delete_profile_by_id(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    _: int = Depends(get_current_user_id),
):
    profile = await db.get(models.Profile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    await db.delete(profile)
    await db.commit()
    return
