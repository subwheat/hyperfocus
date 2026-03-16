from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import AuthContext, CurrentWorkspace
from ..models import SharedRoom, SharedRoomMessage, RoomPresence, get_db
from ..schemas import (
    APIResponse,
    PresenceHeartbeat,
    SharedRoomMessageCreate,
    SharedRoomMessageResponse,
    SharedRoomPresenceResponse,
    SharedRoomStateResponse,
)

router = APIRouter(prefix="/rooms", tags=["Rooms"])


def _participant_label(key: str) -> str:
    return {
        "julien": "Julien",
        "nico": "Nico",
        "assistant": "ACP",
    }.get(key, key.title())


async def _ensure_room(db: AsyncSession, room_id: str) -> SharedRoom:
    room = await db.get(SharedRoom, room_id)
    if room:
        return room
    room = SharedRoom(
        id=room_id,
        name="Room principale" if room_id == "main" else f"Room {room_id}",
    )
    db.add(room)
    await db.commit()
    await db.refresh(room)
    return room


def _collapse_presence(rows: list[RoomPresence]) -> list[SharedRoomPresenceResponse]:
    latest_by_participant = {}
    now = datetime.utcnow()
    online_threshold = now - timedelta(seconds=45)

    for row in rows:
        prev = latest_by_participant.get(row.participant_key)
        if not prev or (row.last_seen_at or datetime.min) > (prev.last_seen_at or datetime.min):
            latest_by_participant[row.participant_key] = row

    collapsed = []
    for row in latest_by_participant.values():
        last_seen = row.last_seen_at or now
        collapsed.append(
            SharedRoomPresenceResponse(
                participant_key=row.participant_key,
                participant_label=row.participant_label,
                client_id=row.client_id,
                last_seen_at=last_seen,
                is_online=last_seen >= online_threshold,
            )
        )
    collapsed.sort(key=lambda x: x.participant_key)
    return collapsed


@router.get("/{room_id}", response_model=SharedRoomStateResponse)
async def get_room_state(
    room_id: str,
    after_id: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
    auth: AuthContext = CurrentWorkspace,
    db: AsyncSession = Depends(get_db),
):
    room = await _ensure_room(db, room_id)

    stmt = (
        select(SharedRoomMessage)
        .where(SharedRoomMessage.room_id == room_id)
        .order_by(SharedRoomMessage.id.asc())
        .limit(limit)
    )
    if after_id > 0:
        stmt = stmt.where(SharedRoomMessage.id > after_id)

    messages = (await db.execute(stmt)).scalars().all()
    presence_rows = (
        await db.execute(select(RoomPresence).where(RoomPresence.room_id == room_id))
    ).scalars().all()

    last_message_id = messages[-1].id if messages else after_id

    return SharedRoomStateResponse(
        room_id=room.id,
        room_name=room.name,
        messages=[
            SharedRoomMessageResponse(
                id=m.id,
                room_id=m.room_id,
                role=m.role,
                author_key=m.author_key,
                author_label=m.author_label,
                message_type=m.message_type,
                content=m.content,
                model=m.model,
                created_at=m.created_at,
            )
            for m in messages
        ],
        online=_collapse_presence(presence_rows),
        last_message_id=last_message_id,
    )


@router.post("/{room_id}/messages", response_model=SharedRoomMessageResponse)
async def create_room_message(
    room_id: str,
    payload: SharedRoomMessageCreate,
    auth: AuthContext = CurrentWorkspace,
    db: AsyncSession = Depends(get_db),
):
    await _ensure_room(db, room_id)

    msg = SharedRoomMessage(
        room_id=room_id,
        role=payload.role,
        author_key=payload.author_key,
        author_label=payload.author_label or _participant_label(payload.author_key),
        message_type=payload.message_type,
        content=payload.content,
        model=payload.model,
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return msg


@router.post("/{room_id}/presence", response_model=APIResponse)
async def heartbeat_room_presence(
    room_id: str,
    payload: PresenceHeartbeat,
    request: Request,
    auth: AuthContext = CurrentWorkspace,
    db: AsyncSession = Depends(get_db),
):
    await _ensure_room(db, room_id)

    existing = (
        await db.execute(
            select(RoomPresence).where(
                RoomPresence.room_id == room_id,
                RoomPresence.client_id == payload.client_id,
            )
        )
    ).scalar_one_or_none()

    now = datetime.utcnow()

    if existing:
        existing.participant_key = payload.participant_key
        existing.participant_label = payload.participant_label or _participant_label(payload.participant_key)
        existing.last_seen_at = now
        existing.user_agent = request.headers.get("user-agent")
        existing.ip_address = request.client.host if request.client else None
    else:
        existing = RoomPresence(
            room_id=room_id,
            client_id=payload.client_id,
            participant_key=payload.participant_key,
            participant_label=payload.participant_label or _participant_label(payload.participant_key),
            last_seen_at=now,
            user_agent=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
        )
        db.add(existing)

    stale_cutoff = now - timedelta(days=2)
    await db.execute(
        delete(RoomPresence).where(
            RoomPresence.room_id == room_id,
            RoomPresence.last_seen_at < stale_cutoff,
        )
    )

    await db.commit()

    return APIResponse(
        success=True,
        data={
            "room_id": room_id,
            "participant_key": existing.participant_key,
            "participant_label": existing.participant_label,
            "last_seen_at": now.isoformat(),
        },
    )
