"""Task 4: read-only payment API.

`GET /api/budgets/{department}` exposes `BudgetStore.get_remaining` for the
smoke harness (INV-1014 no-overspend checks) and any operator/UI wanting a
department's live remaining budget without going through the saga.
"""

from fastapi import APIRouter, Depends, HTTPException

from .budgets import BudgetStore
from .deps import get_budget_store

router = APIRouter(prefix="/api")


@router.get("/budgets/{department}")
async def get_budget(department: str, budgets: BudgetStore = Depends(get_budget_store)) -> dict:
    remaining = await budgets.get_remaining(department)
    if remaining is None:
        raise HTTPException(status_code=404, detail=f"unknown department {department}")
    return {"department": department, "remaining_cents": remaining}
