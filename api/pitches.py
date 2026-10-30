import json
import logging
import os
import queue
import threading
import time

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from api import usage_limits
from api.paging import MAX_OFFSET
from api.auth import get_completed_user
from api.contact_access import allow_contacts
from api.schemas import (
    ManualPitchRequest,
    PitchListResponse,
    PitchRequest,
    PitchResponse,
    PitchUpdateRequest,
    pitch_to_response,
)
from pipeline.db import BrandRaw, CreatorProfile, Pitch, SessionLocal, get_db
from pipeline.helpers.gpt_llm import LLMUnavailableError
from pipeline.own_brand import claim_followup, run_followup
from pipeline.pitching import generate_pitch, stream_pitch

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pitches", tags=["pitches"])

def stream_deadline_seconds() -> int:
    """PITCH_STREAM_DEADLINE_SECONDS (default 150): the longest a pitch stream may run, kept under the browser proxy's 180 s limit."""
    raw = (os.environ.get("PITCH_STREAM_DEADLINE_SECONDS") or "").strip()
    if not raw:
        return 150
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"PITCH_STREAM_DEADLINE_SECONDS must be a whole number of seconds, got {raw!r}")
    if value < 1:
        raise ValueError(f"PITCH_STREAM_DEADLINE_SECONDS must be >= 1, got {value}")
    return value


stream_deadline_seconds()   # a bad setting stops the application at start

PITCH_UNAVAILABLE = "Pitch generation is temporarily unavailable, try again"   # shown instead of any provider / internal error text

_ALLOWED_STATUSES = {"generated", "sent", "negotiating", "closed_won", "closed_lost", "declined"}
_STATUS_TRANSITIONS = {
    "generated": {"generated", "sent"},
    "sent": {"sent", "negotiating", "closed_won", "closed_lost", "declined"},
    "negotiating": {"negotiating", "closed_won", "closed_lost", "declined"},
    "closed_won": {"closed_won"},
    "closed_lost": {"closed_lost"},
    "declined": {"declined"},
}


@router.post("", response_model=PitchResponse)
def create_pitch(
    body: PitchRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_completed_user),
):
    if body.is_custom:
        if not body.custom_brand_name or body.brand_id is not None:
            raise HTTPException(status_code=400, detail="custom_brand_name is required (and brand_id must be omitted) for a custom pitch")
    elif body.brand_id is None:
        raise HTTPException(status_code=400, detail="brand_id is required for a non-custom pitch")

    creator_id = current_user.id
    lock_bind = db.get_bind()
    usage_limits.acquire_lock(db, creator_id)   # 409 while another pitch of this user is being written
    try:
        usage_limits.consume(db, creator_id, "pitches")   # 429 over the daily cap, before any LLM call
        include_contact = allow_contacts(db, creator_id, body.brand_id)   # False (no contact in the pitch) for a brand the creator may not see; 429 over the daily cap
        try:
            pitch = generate_pitch(
                db, current_user,
                include_contact=include_contact,
                is_custom=body.is_custom,
                brand_id=body.brand_id,
                custom_brand_name=body.custom_brand_name,
                story=body.story,
                product_reference=body.product_reference,
                past_brand_partnership=body.past_brand_partnership,
                content_link=body.content_link,
            )
        except ValueError as exc:   # a brand that does not exist
            raise HTTPException(status_code=404, detail=str(exc))
        except RuntimeError as exc:
            logger.warning("Pitch generation failed for creator_id=%s: %s", creator_id, exc)
            raise HTTPException(status_code=502, detail=PITCH_UNAVAILABLE)
        # A brand the user added through "my own brand": now that the pitch exists, run the rest of its pipeline.
        followup_job = claim_followup(db, creator_id, pitch.brand_raw_id)
        if followup_job:
            background_tasks.add_task(run_followup, followup_job)
        return pitch_to_response(pitch)
    finally:
        usage_limits.release_lock(lock_bind, creator_id)


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.post("/stream")
def stream_pitch_endpoint(
    body: PitchRequest,
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_completed_user),
):
    """
    Same as POST /pitches (same body, same prompt, saved as a generated, unsent pitch), but the
    email is sent while the LLM writes it, as server-sent events (text/event-stream), one per line:
      {"type": "status", "text": "..."}    what is happening before the text starts (e.g. reading the linked posts)
      {"type": "delta", "text": "..."}     a piece of the email, in order
      {"type": "done", "pitch": {...}}     the saved pitch (PitchResponse), last event on success
      {"type": "error", "detail": "..."}   generation failed; nothing was saved
    Request errors (400 / 404) are returned as normal HTTP errors before the stream starts.
    """
    if body.is_custom:
        if not body.custom_brand_name or body.brand_id is not None:
            raise HTTPException(status_code=400, detail="custom_brand_name is required (and brand_id must be omitted) for a custom pitch")
    elif body.brand_id is None:
        raise HTTPException(status_code=400, detail="brand_id is required for a non-custom pitch")
    elif not db.query(BrandRaw.id).filter(BrandRaw.id == body.brand_id).first():
        raise HTTPException(status_code=404, detail=f"brand_id={body.brand_id} not found")
    creator_id = current_user.id
    lock_bind = db.get_bind()
    usage_limits.acquire_lock(db, creator_id)   # 409 while another pitch of this user is being written
    try:
        usage_limits.consume(db, creator_id, "pitches")   # 429 over the daily cap, before the stream starts and before any LLM call
        include_contact = allow_contacts(db, creator_id, body.brand_id)   # commits; 429 over the daily cap, before the stream starts
        db.commit()   # the request session only validated; end its transaction so it is not open for the whole stream
    except BaseException:
        usage_limits.release_lock(lock_bind, creator_id)
        raise
    followups: list[str] = []   # filled once the pitch is saved; run after the response has been sent

    def start_followups():
        usage_limits.release_lock(lock_bind, creator_id)   # in case the stream was cut before its own cleanup ran
        for job_id in followups:
            run_followup(job_id)

    cancel = threading.Event()
    finished = object()   # the worker is done

    def worker(out: "queue.Queue") -> None:
        """Runs the pitch stream on its own thread and puts the SSE events (dicts) on `out`, so the response can give up at the deadline."""
        try:
            # its own session: the request's session may be closed before the stream ends
            with SessionLocal() as stream_db:
                creator = stream_db.query(CreatorProfile).filter(CreatorProfile.id == creator_id).first()
                try:
                    for kind, value in stream_pitch(
                        stream_db, creator,
                        include_contact=include_contact,
                        is_custom=body.is_custom,
                        brand_id=body.brand_id,
                        custom_brand_name=body.custom_brand_name,
                        story=body.story,
                        product_reference=body.product_reference,
                        past_brand_partnership=body.past_brand_partnership,
                        content_link=body.content_link,
                    ):
                        if cancel.is_set():   # the deadline passed or the browser left: stop reading from the LLM
                            return
                        if kind == "status":
                            out.put({"type": "status", "text": value})
                        elif kind == "delta":
                            out.put({"type": "delta", "text": value})
                        else:
                            job_id = claim_followup(stream_db, creator_id, value.brand_raw_id)
                            if job_id:
                                followups.append(job_id)
                            out.put({"type": "done", "pitch": json.loads(pitch_to_response(value).model_dump_json())})
                except ValueError as exc:   # a brand that does not exist
                    out.put({"type": "error", "detail": str(exc)})
                except (LLMUnavailableError, RuntimeError) as exc:
                    logger.warning("Pitch stream failed for creator_id=%s: %s", creator_id, exc)
                    out.put({"type": "error", "detail": PITCH_UNAVAILABLE})
                except DBAPIError:
                    # the response has already started, so no 503: report it in-stream without the SQL text
                    logger.exception("Pitch stream database error for creator_id=%s", creator_id)
                    out.put({"type": "error", "detail": "Database busy, retry"})
        except Exception:
            logger.exception("Pitch stream crashed for creator_id=%s", creator_id)
            out.put({"type": "error", "detail": PITCH_UNAVAILABLE})
        finally:
            out.put(finished)

    def events():
        out: "queue.Queue" = queue.Queue()
        deadline = time.monotonic() + stream_deadline_seconds()
        threading.Thread(target=worker, args=(out,), name=f"pitch-stream-{creator_id}", daemon=True).start()
        try:
            while True:
                try:
                    item = out.get(timeout=max(deadline - time.monotonic(), 0))
                except queue.Empty:
                    logger.warning("Pitch stream for creator_id=%s hit the %ss deadline and was stopped", creator_id, stream_deadline_seconds())
                    yield _sse({"type": "error", "detail": PITCH_UNAVAILABLE})
                    return
                if item is finished:
                    return
                yield _sse(item)
        finally:
            cancel.set()   # a worker still running stops at its next event
            usage_limits.release_lock(lock_bind, creator_id)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},   # no proxy buffering
        background=BackgroundTask(start_followups),
    )


@router.post("/manual", response_model=PitchResponse)
def create_manual_pitch(
    body: ManualPitchRequest,
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_completed_user),
):
    if body.status not in _ALLOWED_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid pitch status")
    if body.brand_id is None and not body.custom_brand_name:
        raise HTTPException(status_code=400, detail="brand_id or custom_brand_name is required")
    if body.brand_id is not None and body.custom_brand_name is not None:
        raise HTTPException(status_code=400, detail="Provide brand_id or custom_brand_name, not both")

    if body.brand_id is not None:
        brand = db.query(BrandRaw).filter(BrandRaw.id == body.brand_id).first()
        if not brand:
            raise HTTPException(status_code=404, detail=f"brand_id={body.brand_id} not found")
        brand_name = brand.name or brand.instagram_handle or "this brand"
    else:
        brand_name = body.custom_brand_name.strip()
        if not brand_name:
            raise HTTPException(status_code=400, detail="custom_brand_name cannot be blank")

    pitch = Pitch(
        creator_profile_id=current_user.id,
        brand_raw_id=body.brand_id,
        is_custom=body.brand_id is None,
        brand_name=brand_name,
        story="",
        status=body.status,
        sent_at=body.sent_at,
        is_manual=True,
    )
    db.add(pitch)
    db.commit()
    db.refresh(pitch)
    return pitch_to_response(pitch)


@router.patch("/{pitch_id}", response_model=PitchResponse)
def update_pitch(
    pitch_id: int,
    body: PitchUpdateRequest,
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_completed_user),
):
    pitch = db.query(Pitch).filter(Pitch.id == pitch_id).first()
    if not pitch:
        raise HTTPException(status_code=404, detail="Pitch not found")
    if pitch.creator_profile_id != current_user.id:   # the same answer as for a missing id: the ids of other creators' pitches are not revealed
        raise HTTPException(status_code=404, detail="Pitch not found")
    if not body.model_fields_set:
        raise HTTPException(status_code=400, detail="At least one field is required")
    if body.pitch_text is not None:
        if pitch.is_manual:
            raise HTTPException(status_code=409, detail="A manually recorded pitch has no email text to edit")
        pitch.pitch_text = body.pitch_text   # the exact text the creator edited

    if body.status is not None:
        if body.status not in _ALLOWED_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid pitch status")
        allowed_next_statuses = _STATUS_TRANSITIONS.get(pitch.status)
        if allowed_next_statuses is None or body.status not in allowed_next_statuses:
            raise HTTPException(status_code=409, detail=f"Cannot transition from {pitch.status} to {body.status}")
        if body.status == "sent" and pitch.sent_at is None and body.sent_at is None:
            raise HTTPException(status_code=400, detail="sent_at is required when marking a pitch sent")
        pitch.status = body.status
    if "sent_at" in body.model_fields_set:
        if body.sent_at is None and pitch.status != "generated":
            raise HTTPException(status_code=409, detail="sent_at cannot be cleared after a pitch is sent")
        if body.sent_at is not None and body.status not in {"sent", "negotiating", "closed_won", "closed_lost", "declined"} and pitch.status == "generated":
            raise HTTPException(status_code=400, detail="sent_at requires a sent or later pitch status")
        pitch.sent_at = body.sent_at
    if "agreed_rate" in body.model_fields_set:
        pitch.agreed_rate = body.agreed_rate

    db.commit()
    db.refresh(pitch)
    return pitch_to_response(pitch)


@router.delete("/{pitch_id}")
def delete_pitch(
    pitch_id: int,
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_completed_user),
):
    """Deletes one of the creator's own pitches (the Pipeline's Delete button)."""
    pitch = db.query(Pitch).filter(Pitch.id == pitch_id).first()
    if not pitch:
        raise HTTPException(status_code=404, detail="Pitch not found")
    if pitch.creator_profile_id != current_user.id:
        raise HTTPException(status_code=404, detail="Pitch not found")
    db.delete(pitch)
    db.commit()
    return {"id": pitch_id, "deleted": True}


@router.get("/me", response_model=PitchListResponse)
def list_pitches(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_completed_user),
):
    query = db.query(Pitch).filter(Pitch.creator_profile_id == current_user.id)
    total = query.count()
    rows = (
        query
        .order_by(Pitch.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return PitchListResponse(
        pitches=[pitch_to_response(p) for p in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
