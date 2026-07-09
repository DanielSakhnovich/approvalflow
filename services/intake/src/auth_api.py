from afcommon.auth import SEED_USERS, create_access_token
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/auth")


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest) -> LoginResponse:
    user = SEED_USERS.get(body.username)
    # Plain-text password compare is acceptable ONLY for these seeded demo
    # credentials -- not a pattern to carry over to a real user store.
    if user is None or user["password"] != body.password:
        raise HTTPException(status_code=401, detail="invalid username or password")
    token = create_access_token(sub=body.username, role=user["role"])
    return LoginResponse(access_token=token, role=user["role"])
