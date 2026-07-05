from fastapi import APIRouter, Depends, HTTPException

from .config import ConfigRepo, Thresholds
from .deps import get_config_repo

router = APIRouter(prefix="/api/config")


@router.get("/thresholds")
async def get_thresholds(repo: ConfigRepo = Depends(get_config_repo)) -> Thresholds:
    return await repo.get_thresholds()


@router.put("/thresholds")
async def update_thresholds(
    partial: dict, repo: ConfigRepo = Depends(get_config_repo)
) -> Thresholds:
    try:
        return await repo.update_thresholds(partial)
    except ValueError as e:
        raise HTTPException(status_code=422, detail="unknown threshold keys") from e
