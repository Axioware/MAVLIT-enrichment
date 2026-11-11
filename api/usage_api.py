"""GET /usage/summary: the numbers of the admin "Usage & Cost" page as JSON, for the frontend page. Admin accounts only (role = "admin")."""
from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from api import usage_dashboard
from api.auth import get_admin_user
from pipeline.db import CreatorProfile, get_db

router = APIRouter(prefix="/usage", tags=["usage"])


@router.get("/summary")
def usage_summary(days: int = 30, user: int | None = None, db: Session = Depends(get_db), _admin: CreatorProfile = Depends(get_admin_user)):
    """?days=7|30|90 (anything else means 30); ?user=<creator id> adds that user's own breakdown."""
    return jsonable_encoder(usage_dashboard.load(db, usage_dashboard.parse_days(days), user))
