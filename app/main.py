"""
Микросервис управления профилями разработчиков.
"""

import os
from typing import Optional
import time
from metrics import REQUEST_COUNT, REQUEST_LATENCY
from starlette.middleware.base import BaseHTTPMiddleware
from app import models, schemas
from app.database import get_db
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
import logging
import json
from datetime import datetime

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from jose import ExpiredSignatureError, JWTError, jwt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


app = FastAPI(
    title="Profile Service API",
    description="Сервис для управления профилями разработчиков",
    version="1.0.0",
)

class MetricsMiddleware(BaseHTTPMiddleware):
    """
    Middleware для сбора метрик по каждому запросу.
    Выполняется для каждого запроса автоматически.
    """
    async def dispatch(self, request: Request, call_next):
    # Запоминаем время начала обработки запроса
        start = time.time()
        # Передаём запрос дальше (в эндпоинт)
        response = await call_next(request)
        # Вычисляем, сколько времени заняла обработка
        duration = time.time() - start
        # Увеличиваем счётчик запросов
        # .labels() позволяет указать значения меток
        REQUEST_COUNT.labels(
        method=request.method,
        endpoint=request.url.path,
        status=response.status_code
        ).inc()
        # Записываем время выполнения в гистограмму
        REQUEST_LATENCY.labels(
        method=request.method,
        endpoint=request.url.path
        ).observe(duration)
        return response

app.add_middleware(MetricsMiddleware)


_LOG_RECORD_SKIP = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs",
    "message", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
})


class JSONFormatter(logging.Formatter):
    """
    Форматтер, который выводит логи в виде JSON.
    Поля из extra= попадают в корень JSON-объекта.
    """
    def format(self, record):
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _LOG_RECORD_SKIP:
                log_entry[key] = value
        return json.dumps(log_entry)
# Настройка корневого логгера
handler = logging.StreamHandler() # выводим в консоль
handler.setFormatter(JSONFormatter()) # используем JSON-форматтер
logging.root.addHandler(handler)
logging.root.setLevel(logging.INFO) # уровень INFO (WARNING, ERROR и выше тоже выводятся)
# Создаём логгер для текущего модуля
logger = logging.getLogger(__name__)

INTEGRATIONS_BASE_URL = os.getenv("INTEGRATIONS_BASE_URL", "http://integrations:8004")
AUTH_SERVICE_URL = os.getenv("AUTH_SERVICE_URL", "http://auth-service:8000")
JWT_SECRET = os.getenv("JWT_SECRET", "change-me-in-env")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
PUBLIC_PATHS = {
    "/",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/metrics",
    "/test/error",
    "/test/slow",
}


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


async def get_current_profile(db: AsyncSession, user_id: int) -> models.Profile:
    stmt = select(models.Profile).where(models.Profile.user_id == user_id)
    result = await db.execute(stmt)
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


def _integrations_internal_headers(profile_id: int) -> dict:
    return {"X-Internal-User-Id": str(profile_id)}


def _integrations_json_response(response: httpx.Response) -> JSONResponse:
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            return JSONResponse(status_code=response.status_code, content=response.json())
        except Exception:
            pass
    return JSONResponse(
        status_code=response.status_code,
        content={"detail": response.text},
    )


# ----- Корневой эндпоинт -----


@app.get("/")
def root():
    logger.info("Root endpoint requested")
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
    logger.info("Creating profile", extra={"user_id": user_id})
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
    logger.info("Profile created", extra={"profile_id": db_profile.id, "user_id": user_id})
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
    logger.info("Fetching monkeytype stats", extra={"user_id": user_id, "username": body.username})
    url = f"{INTEGRATIONS_BASE_URL}/integrations/monkeytype/fetch"
    stmt = select(models.Profile).where(models.Profile.user_id == user_id)
    result = await db.execute(stmt)
    profile = result.scalar_one_or_none()
    if not profile:
        logger.warning("Profile not found for monkeytype fetch", extra={"user_id": user_id})
        raise HTTPException(status_code=404, detail="Profile not found")

    payload = {
        "profile_id": profile.id,
        "username": body.username,
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(url, json=payload)
    except httpx.HTTPError:
        logger.error("Integrations service unavailable (monkeytype)", extra={"user_id": user_id})
        raise HTTPException(
            status_code=503,
            detail="Integrations service is unavailable",
        )

    logger.info(
        "Monkeytype fetch completed",
        extra={"profile_id": profile.id, "status_code": response.status_code},
    )
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        return JSONResponse(status_code=response.status_code, content=response.json())
    return JSONResponse(
        status_code=response.status_code,
        content={"detail": response.text},
    )


@app.post("/profile/integrations/github/connect")
async def github_connect_via_integrations(
    body: schemas.GithubConnectProxyRequest,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    """
    Прокси на POST /integrations/github/connect integrations-service.
    Передаёт username (GitHub) и internal_user_id = id профиля в profile-service.
    """
    logger.info("Connecting GitHub integration", extra={"user_id": user_id, "username": body.username})
    profile = await get_current_profile(db, user_id)
    url = f"{INTEGRATIONS_BASE_URL}/integrations/github/connect"
    payload = {
        "username": body.username,
        "internal_user_id": profile.id,
    }
    headers = _integrations_internal_headers(profile.id)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError:
        logger.error("Integrations service unavailable (github connect)", extra={"profile_id": profile.id})
        raise HTTPException(
            status_code=503,
            detail="Integrations service is unavailable",
        )
    logger.info(
        "GitHub connect completed",
        extra={"profile_id": profile.id, "status_code": response.status_code},
    )
    return _integrations_json_response(response)


@app.get("/profile/integrations/github/private-stats")
async def github_private_stats_via_integrations(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    """Прокси на GET /integrations/github/private-stats с X-Internal-User-Id."""
    logger.info("Fetching GitHub private stats", extra={"user_id": user_id})
    profile = await get_current_profile(db, user_id)
    url = f"{INTEGRATIONS_BASE_URL}/integrations/github/private-stats"
    headers = _integrations_internal_headers(profile.id)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError:
        logger.error("Integrations service unavailable (github stats)", extra={"profile_id": profile.id})
        raise HTTPException(
            status_code=503,
            detail="Integrations service is unavailable",
        )
    logger.info(
        "GitHub private stats fetched",
        extra={"profile_id": profile.id, "status_code": response.status_code},
    )
    return _integrations_json_response(response)


@app.delete("/profile/integrations/{provider}")
async def integrations_disconnect_via_integrations(
    provider: str,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    """Прокси на DELETE /integrations/{provider} с X-Internal-User-Id."""
    logger.info("Disconnecting integration", extra={"user_id": user_id, "provider": provider})
    profile = await get_current_profile(db, user_id)
    url = f"{INTEGRATIONS_BASE_URL}/integrations/{provider}"
    headers = _integrations_internal_headers(profile.id)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.delete(url, headers=headers)
    except httpx.HTTPError:
        logger.error(
            "Integrations service unavailable (disconnect)",
            extra={"profile_id": profile.id, "provider": provider},
        )
        raise HTTPException(
            status_code=503,
            detail="Integrations service is unavailable",
        )
    logger.info(
        "Integration disconnected",
        extra={"profile_id": profile.id, "provider": provider, "status_code": response.status_code},
    )
    if response.status_code == 204:
        return Response(status_code=204)
    return _integrations_json_response(response)


# ----- GET /profile/me — профиль текущего пользователя -----


@app.get("/profile/me", response_model=schemas.ProfileResponse)
async def read_profile_me(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    logger.info("Reading current user profile", extra={"user_id": user_id})
    stmt = select(models.Profile).where(models.Profile.user_id == user_id)
    result = await db.execute(stmt)
    profile = result.scalar_one_or_none()
    if not profile:
        logger.warning("Profile not found", extra={"user_id": user_id})
        raise HTTPException(status_code=404, detail="Profile not found")
    logger.info("Profile read", extra={"profile_id": profile.id, "user_id": user_id})
    return profile


# ----- PATCH /profile/me — обновить свой профиль -----


@app.patch("/profile/me", response_model=schemas.ProfileResponse) 
async def update_profile_me(
    profile_update: schemas.ProfileUpdate,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    logger.info("Updating current user profile", extra={"user_id": user_id})
    stmt = select(models.Profile).where(models.Profile.user_id == user_id)
    result = await db.execute(stmt)
    profile = result.scalar_one_or_none()
    if not profile:
        logger.warning("Profile not found for update", extra={"user_id": user_id})
        raise HTTPException(status_code=404, detail="Profile not found")

    update_data = profile_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(profile, field, value)

    await db.commit()
    await db.refresh(profile)
    logger.info("Profile updated", extra={"profile_id": profile.id, "user_id": user_id})
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
    logger.info("Reading profile by username", extra={"username": username})
    stmt = select(models.Profile).where(models.Profile.username == username)
    result = await db.execute(stmt)
    profile = result.scalar_one_or_none()
    if not profile:
        logger.warning("Profile not found by username", extra={"username": username})
        raise HTTPException(status_code=404, detail="Profile not found")
    logger.info("Profile read by username", extra={"profile_id": profile.id, "username": username})
    return profile


# ----- GET /profile/{profile_id} — профиль по ID -----


@app.get("/profile/{profile_id}", response_model=schemas.ProfileResponse)
async def read_profile_by_id(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    _: int = Depends(get_current_user_id),
):
    logger.info("Reading profile by id", extra={"profile_id": profile_id})
    profile = await db.get(models.Profile, profile_id)
    if not profile:
        logger.warning("Profile not found by id", extra={"profile_id": profile_id})
        raise HTTPException(status_code=404, detail="Profile not found")
    logger.info("Profile read by id", extra={"profile_id": profile.id})
    return profile


# ----- PATCH /profile/{profile_id} — обновить по ID (админ/внутренние сервисы) -----


@app.patch("/profile/{profile_id}", response_model=schemas.ProfileResponse)
async def update_profile_by_id(
    profile_id: int,
    profile_update: schemas.ProfileUpdate,
    db: AsyncSession = Depends(get_db),
    _: int = Depends(get_current_user_id),
):
    logger.info("Updating profile by id", extra={"profile_id": profile_id})
    profile = await db.get(models.Profile, profile_id)
    if not profile:
        logger.warning("Profile not found for update by id", extra={"profile_id": profile_id})
        raise HTTPException(status_code=404, detail="Profile not found")

    update_data = profile_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(profile, field, value)

    await db.commit()
    await db.refresh(profile)
    logger.info("Profile updated by id", extra={"profile_id": profile.id})
    return profile


# ----- DELETE /profile/me — удалить свой профиль -----


@app.delete("/profile/me", status_code=204)
async def delete_profile_me(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
):
    logger.info("Deleting current user profile", extra={"user_id": user_id})
    stmt = select(models.Profile).where(models.Profile.user_id == user_id)
    result = await db.execute(stmt)
    profile = result.scalar_one_or_none()
    if not profile:
        logger.warning("Profile not found for delete", extra={"user_id": user_id})
        raise HTTPException(status_code=404, detail="Profile not found")

    await db.delete(profile)
    await db.commit()
    logger.info("Profile deleted", extra={"profile_id": profile.id, "user_id": user_id})
    return


# ----- DELETE /profile/{profile_id} — удалить по ID (админ/внутренние сервисы) -----


@app.delete("/profile/{profile_id}", status_code=204)
async def delete_profile_by_id(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    _: int = Depends(get_current_user_id),
):
    logger.info("Deleting profile by id", extra={"profile_id": profile_id})
    profile = await db.get(models.Profile, profile_id)
    if not profile:
        logger.warning("Profile not found for delete by id", extra={"profile_id": profile_id})
        raise HTTPException(status_code=404, detail="Profile not found")

    await db.delete(profile)
    await db.commit()
    logger.info("Profile deleted by id", extra={"profile_id": profile_id})
    return


# ----- GET /metrics — метрики -----

@app.get("/metrics")
async def get_metrics():
    """
    Эндпоинт для Prometheus.
    Prometheus будет заходить сюда каждые 15 секунд и забирать метрики.
    """
    logger.info("Metrics endpoint requested")
    # generate_latest() возвращает все метрики в текстовом формате
    # CONTENT_TYPE_LATEST — правильный MIME-тип для Prometheus
    return Response(content=generate_latest(),
    media_type=CONTENT_TYPE_LATEST)


@app.get("/test/error")
async def test_error():
    """
    Тестовый эндпоинт, который всегда возвращает ошибку 500.
    Нужен для проверки метрики error rate.
    """
    raise HTTPException(status_code=500, detail="Тестовая ошибка")

@app.get("/test/slow")
async def test_slow():
    """
    Тестовый эндпоинт, который имитирует долгую обработку (2 секунды).
    Нужен для проверки метрики latency.
    """
    import time
    time.sleep(2)
    return {"status": "ok", "message": "Медленный ответ после 2 секунд"}