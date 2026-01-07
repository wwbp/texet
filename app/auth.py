import os
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_security = HTTPBearer(auto_error=False)


def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
) -> None:
    token = os.getenv("API_TOKEN")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API_TOKEN is not set.",
        )

    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if credentials.scheme.lower() != "bearer" or not secrets.compare_digest(
        credentials.credentials, token
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized.",
            headers={"WWW-Authenticate": "Bearer"},
        )
