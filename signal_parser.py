from __future__ import annotations
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class ParsedSignal:
    symbol: str
    direction: str  # BUY or SELL
    lot_size: float  # 0.0 if not provided in signal
    zone_low: float
    zone_high: float
    stop_loss: float
    take_profits: List[Optional[float]]  # numeric TPs; None for 'open'
    original_text: str

    @property
    def zone_mid(self) -> float:
        return (self.zone_low + self.zone_high) / 2.0


_re_lot = re.compile(r"lot\s*size\s*[:\-]?\s*([0-9]*\.?[0-9]+)", re.I)
_re_pair_dir = re.compile(r"([A-Z0-9]+)\s+LOOKING\s+(BUY|SELL)\s+THIS\s+ZONE", re.I)
_re_zone = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)")
_re_sl = re.compile(r"SL\s*[:\-]?\s*(\d+(?:\.\d+)?)", re.I)
_re_tp = re.compile(r"TP\s*([1-9][0-9]*)\s*[:\-]?\s*([0-9]+(?:\.[0-9]+)?|open)", re.I)


def parse_signal(text: str) -> ParsedSignal | None:
    if not text or not isinstance(text, str):
        return None

    m_lot = _re_lot.search(text)
    m_pair = _re_pair_dir.search(text)
    m_zone = _re_zone.search(text)
    m_sl = _re_sl.search(text)
    tps = _re_tp.findall(text)

    # Lot size is optional; others required
    if not (m_pair and m_zone and m_sl and tps):
        return None

    lot = float(m_lot.group(1)) if m_lot else 0.0
    symbol = m_pair.group(1).upper()
    direction = m_pair.group(2).upper()
    z1 = float(m_zone.group(1))
    z2 = float(m_zone.group(2))
    zone_low, zone_high = (min(z1, z2), max(z1, z2))
    sl = float(m_sl.group(1))

    # Sort TPs by index; convert values to float or None for 'open'
    tp_pairs: List[Tuple[int, Optional[float]]] = []
    for idx_str, val in tps:
        idx = int(idx_str)
        if val.strip().lower() == 'open':
            tp_pairs.append((idx, None))
        else:
            tp_pairs.append((idx, float(val)))
    tp_pairs.sort(key=lambda x: x[0])
    tp_values = [v for _, v in tp_pairs]

    return ParsedSignal(
        symbol=symbol,
        direction=direction,
        lot_size=lot,
        zone_low=zone_low,
        zone_high=zone_high,
        stop_loss=sl,
        take_profits=tp_values,
        original_text=text,
    )
