"""常用问题库 API — C6"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.state import get_state

router = APIRouter(prefix="/api/v1", tags=["questions"])

class SaveRequest(BaseModel):
    question: str
    sql: str
    tables: list[str] = []
    visibility: str = "private"

@router.get("/questions/{conn_id}")
async def list_questions(conn_id: str):
    state = get_state()
    try:
        state.connections.get(conn_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"entries": state.questions.list(conn_id)}

@router.post("/questions/{conn_id}")
async def save_question(conn_id: str, req: SaveRequest):
    state = get_state()
    try:
        state.connections.get(conn_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    try:
        entry = state.questions.save(conn_id, req.question, req.sql, req.tables, req.visibility)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return entry

@router.delete("/questions/{conn_id}/{qid}")
async def delete_question(conn_id: str, qid: str):
    state = get_state()
    ok = state.questions.delete(conn_id, qid)
    if not ok:
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}

@router.post("/questions/{conn_id}/match")
async def match_question(conn_id: str, body: dict):
    state = get_state()
    q = body.get("question", "")
    tables = body.get("tables")
    m = state.questions.match(conn_id, q, tables)
    return {"matched": m is not None, "entry": m}
