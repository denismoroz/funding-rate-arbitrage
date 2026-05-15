from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from frab.events.bus import Event, EventBus


router = APIRouter()


def _serialize(event: Event) -> dict:
    return {
        "ts": event.ts.isoformat(),
        "level": event.level,
        "source": event.source,
        "kind": event.kind,
        "message": event.message,
        "payload_json": event.payload_json,
    }


@router.websocket("/ws/live")
async def ws_live(websocket: WebSocket) -> None:
    bus: EventBus = websocket.app.state.event_bus
    await websocket.accept()
    try:
        async with bus.subscribe(maxsize=100) as q:
            while True:
                event = await q.get()
                await websocket.send_json(_serialize(event))
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        raise
