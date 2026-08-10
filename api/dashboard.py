from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from api.auth import get_current_user
from api.schemas import DashboardResponse
from pipeline.dashboard import get_dashboard_summary
from pipeline.db import CreatorProfile, get_db

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/me", response_model=DashboardResponse)
def get_my_dashboard(
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_current_user),
):
    return DashboardResponse(**get_dashboard_summary(db, current_user))
