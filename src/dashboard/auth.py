"""Единая auth-зависимость: HTTPBasic для HTML, Bearer для API."""
import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from config import settings


basic = HTTPBasic(auto_error=False)


def require_auth(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(basic),
) -> str:
    """
    Пропускает запрос если:
      - есть валидный Authorization: Bearer <DASHBOARD_TOKEN>
      - ИЛИ Basic admin:<DASHBOARD_TOKEN>
    """
    token = settings.dashboard_token

    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        provided = auth_header.split(" ", 1)[1].strip()
        if secrets.compare_digest(provided, token):
            return "api"
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid bearer")

    if credentials is not None:
        ok_user = secrets.compare_digest(credentials.username, "admin")
        ok_pass = secrets.compare_digest(credentials.password, token)
        if ok_user and ok_pass:
            return "ui"

    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "auth required",
        headers={"WWW-Authenticate": 'Basic realm="dashboard"'},
    )
