"""Loads per-merchant configuration from YAML."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "config" / "merchants.yaml"


@dataclass(frozen=True)
class Merchant:
    id: str
    name: str
    status: str
    payment_methods: tuple[str, ...]
    currencies: tuple[str, ...]
    max_amount: int
    refund_allowed: bool
    callback_url: str
    webhook_secret: str

    def to_dict(self) -> dict:
        return asdict(self)


def load_merchants(path: str | Path | None = None) -> dict[str, Merchant]:
    path = Path(path or os.environ.get("MERCHANT_CONFIG") or DEFAULT_CONFIG)
    raw = yaml.safe_load(path.read_text())["merchants"]
    return {
        mid: Merchant(
            id=mid,
            name=m["name"],
            status=m["status"],
            payment_methods=tuple(m["payment_methods"]),
            currencies=tuple(m["currencies"]),
            max_amount=int(m["max_amount"]),
            refund_allowed=bool(m["refund_allowed"]),
            callback_url=m["callback_url"],
            webhook_secret=m["webhook_secret"],
        )
        for mid, m in raw.items()
    }
