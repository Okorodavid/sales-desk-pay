"""Shelf camera: SAM 3.1 counts what is on a shop shelf from a photo or a CCTV frame.

Each snapshot is counted with one short phrase. When the count drops, the desk compares it with the paid orders
for that shelf's products since the last snapshot, so the owner sees stock that left the shelf without a sale.
It only counts and compares; it never changes the price list or accuses anyone."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from .meta import MetaClient
from .store import UTC

log = logging.getLogger("salesdesk.shelf")
PAID = ("paid", "delivered")
MIN_AREA = 0.01   # a match smaller than 1% of the frame is noise
EDGE = 0.003      # a match cut off by the frame edge belongs to the next shelf


@dataclass
class Shelf:
    name: str = "Ankara shelf"
    phrase: str = "stack of folded fabric on the shelf"
    piece: str = "piece"            # what one counted item is called
    words: tuple[str, ...] = ("ankara",)  # products on this shelf, matched in order item names


@dataclass
class Snapshot:
    at: datetime
    count: int
    boxes: list[tuple[float, float, float, float]]
    image: str  # URL of the saved frame


@dataclass
class ShelfCam:
    shelf: Shelf = field(default_factory=Shelf)
    snaps: list[Snapshot] = field(default_factory=list)

    async def snap(self, client: MetaClient, image: bytes, at: datetime, url: str) -> Snapshot:
        boxes = keep(await client.segment(image, self.shelf.phrase))
        s = Snapshot(at, len(boxes), boxes, url)
        self.snaps.append(s)
        return s

    def report(self, desk) -> dict:
        """What the shelf screen shows: the latest frame, the count over the day, and any stock gone unsold."""
        sh = self.shelf
        out = {"name": sh.name, "piece": sh.piece, "latest": None, "log": [], "gap": None}
        if not self.snaps:
            return out
        fmt = lambda t: t.astimezone(desk.tz).strftime("%H:%M")  # noqa: E731
        last = self.snaps[-1]
        out["latest"] = {"count": last.count, "time": fmt(last.at), "boxes": last.boxes, "image": last.image}
        prev = None
        for s in self.snaps:
            taken = prev.count - s.count if prev else 0
            out["log"].append({"time": fmt(s.at), "count": s.count, "taken": max(taken, 0)})
            prev = s
        if len(self.snaps) > 1:
            before = self.snaps[-2]
            taken = before.count - last.count
            if taken > 0:
                sold = sold_since(desk, before.at, sh.words)
                out["gap"] = {"since": fmt(before.at), "taken": taken, "sold": sold, "unsold": max(taken - sold, 0)}
        return out


def keep(boxes: list[tuple[float, float, float, float]]) -> list[tuple[float, float, float, float]]:
    """Drop specks and anything the frame cuts off."""
    return [b for b in boxes
            if (b[2] - b[0]) * (b[3] - b[1]) >= MIN_AREA
            and b[0] > EDGE and b[1] > EDGE and b[2] < 1 - EDGE and b[3] < 1 - EDGE]


def sold_since(desk, since: datetime, words: tuple[str, ...]) -> int:
    """Paid orders since `since` with at least one item from this shelf."""
    s = desk.store
    stamp = since.astimezone(UTC).isoformat(timespec="seconds")
    n = 0
    for o in s.orders(PAID):
        if o["paid_at"] and o["paid_at"] >= stamp and any(
                any(w in i["name"].lower() for w in words) for i in s.items(o)):
            n += 1
    return n

