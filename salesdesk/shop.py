"""The shop's facts: shop.json (name, payment, delivery), products.csv (price list) and faq.md."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path


def naira(amount: int) -> str:
    return "₦" + f"{int(amount):,}"


@dataclass
class Product:
    sku: str
    name: str
    unit: str  # yard | set | piece
    price: int  # per unit, in naira
    stock: int
    words: list[str] = field(default_factory=list)

    def amount_text(self, qty: int) -> str:
        unit = self.unit if qty == 1 else self.unit + "s"
        return f"{qty} {unit} of {self.name}"


@dataclass
class Zone:
    id: str
    name: str
    fee: int
    days: str
    areas: list[str]


@dataclass
class Shop:
    name: str
    owner: str
    city: str
    about: str
    hours: str
    pay_to: str
    bulk_over_yards: int
    follow_up_hours: int
    max_follow_ups: int
    order_prefix: str
    products: dict[str, Product]
    zones: dict[str, Zone]
    faq: list[tuple[str, str]]

    def zone_for(self, text: str) -> Zone | None:
        """The delivery zone named in a place or area, e.g. 'Independence Layout' -> Enugu city."""
        low = f" {(text or '').lower()} "
        best, size = None, 0
        for z in self.zones.values():
            for area in z.areas:
                if re.search(r"(?<![a-z])" + re.escape(area) + r"(?![a-z])", low) and len(area) > size:
                    best, size = z, len(area)
        return best

    def keywords(self) -> list[str]:
        """Product words, to help voice-note transcription spell them (aso-oke, gele, ipele)."""
        return sorted({w for p in self.products.values() for w in p.words} | {self.name, self.owner})

    def prices(self) -> set[int]:
        """Every naira amount the robot may say without a quote: unit prices and delivery fees."""
        return {p.price for p in self.products.values()} | {z.fee for z in self.zones.values() if z.fee}

    def facts(self) -> str:
        lines = [f'<fact id="SHOP">{self.name}, {self.city}. {self.about} Opening hours: {self.hours} '
                 f'Payment: bank transfer only; the shop sends the account details.</fact>']
        for p in self.products.values():
            stock = "sold out" if p.stock <= 0 else f"{p.stock} {p.unit}s in stock"
            lines.append(f'<fact id="P:{p.sku}">{p.name}: {naira(p.price)} per {p.unit}, {stock}.</fact>')
            if p.words:
                lines.append(f'<words sku="{p.sku}">{" ".join(p.words)}</words>')
        for z in self.zones.values():
            fee = "free" if not z.fee else naira(z.fee)
            lines.append(f'<fact id="D:{z.id}">Delivery to {z.name} ({", ".join(z.areas[:6])}): {fee}, {z.days}.</fact>')
        for i, (q, a) in enumerate(self.faq, 1):
            lines.append(f'<fact id="F{i}">{q} {a}</fact>')
        return "\n".join(lines)


def load_shop(folder: str | Path) -> Shop:
    folder = Path(folder)
    data = json.loads((folder / "shop.json").read_text(encoding="utf-8"))
    products = {}
    with (folder / "products.csv").open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            sku = row["sku"].strip().upper()
            products[sku] = Product(sku, row["name"].strip(), row.get("unit", "piece").strip() or "piece",
                                    int(row["price"]), int(row.get("stock") or 0),
                                    [w for w in (row.get("words") or "").lower().split() if w])
    zones = {z["id"]: Zone(z["id"], z["name"], int(z.get("fee", 0)), z.get("days", ""),
                           [a.lower() for a in z.get("areas", [])]) for z in data.get("delivery", [])}
    faq = []
    path = folder / "faq.md"
    if path.exists():
        for block in re.split(r"^## ", path.read_text(encoding="utf-8"), flags=re.M):
            if block.strip():
                q, _, a = block.partition("\n")
                faq.append((q.strip(), " ".join(a.split())))
    return Shop(
        name=data["name"], owner=data.get("owner", "the owner"), city=data.get("city", ""),
        about=data.get("about", ""), hours=data.get("hours", ""), pay_to=data.get("pay_to", "[ACCOUNT]"),
        bulk_over_yards=int(data.get("bulk_over_yards", 20)), follow_up_hours=int(data.get("follow_up_hours", 20)),
        max_follow_ups=int(data.get("max_follow_ups", 2)), order_prefix=data.get("order_prefix") or _prefix(data["name"]),
        products=products, zones=zones, faq=faq,
    )


def _prefix(name: str) -> str:
    letters = "".join(w[0] for w in re.findall(r"[A-Za-z]+", name))[:3].upper()
    return letters or "SD"
