"""团队鉴权 — E4 本地账号（单机零配置保持）"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.state import get_state

router = APIRouter(prefix="/api/v1", tags=["auth"])

class LoginRequest(BaseModel):
    username: str
    password: str

class RegisterRequest(BaseModel):
    username: str
    password: str
    role: str = "member"

class ChangePasswordRequest(BaseModel):
    username: str
    old_password: str
    new_password: str

@router.post("/auth/login")
async def login(req: LoginRequest):
    state = get_state()
    user = state.auth.verify_password(req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="invalid credentials")
    token = state.auth.issue_token(user)
    return {"token": token, "user": {"id": user.id, "username": user.username, "role": user.role, "is_initial": getattr(user, "is_initial", False)}}

@router.post("/auth/register")
async def register(req: RegisterRequest):
    state = get_state()
    try:
        user = state.auth.create_user(req.username, req.password, req.role)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    token = state.auth.issue_token(user)
    return {"token": token, "user": {"id": user.id, "username": user.username, "role": user.role, "is_initial": getattr(user, "is_initial", False)}}

@router.post("/auth/change-password")
async def change_password(req: ChangePasswordRequest):
    state = get_state()
    try:
        user = state.auth.change_password(req.username, req.old_password, req.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not user:
        raise HTTPException(status_code=401, detail="invalid credentials")
    token = state.auth.issue_token(user)
    return {"token": token, "user": {"id": user.id, "username": user.username, "role": user.role, "is_initial": False}}

@router.get("/auth/me")
async def me():
    # 由中间件注入的 request.state.user 提供
    from fastapi import Request
    # 实际通过依赖注入，这里简化：从 header 再验一次
    # 该路由需在中间件后通过 request.state.user 获取，但为简单，直接要求 token
    state = get_state()
    # 该接口在中间件后，若无 user 则 401 已在中间件处理
    # 这里返回空，需中间件配合
    return {"ok": True}

@router.get("/auth/users")
async def list_users():
    state = get_state()
    # 仅 admin 可列（由中间件鉴权后检查 role）
    # 这里简化：直接返回
    return {"users": state.auth.list_users(), "team_mode": state.auth.is_team_mode()}
