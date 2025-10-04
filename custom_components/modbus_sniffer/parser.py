"""Utility per l'analisi dei frame Modbus provenienti dallo sniffer UDP."""
from __future__ import annotations

from typing import Iterable, Iterator, List, Optional, Sequence, Tuple


def compute_crc(data: bytes) -> int:
    """Calcola il CRC Modbus RTU del blocco dati."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def _candidate_frame_lengths(function_code: Optional[int], max_len: int) -> List[int]:
    """Restituisce le possibili lunghezze per un frame Modbus con CRC valido."""
    lengths: set[int] = set()
    if function_code is None:
        return list(range(4, max_len + 1))

    fc = function_code & 0x7F

    if fc in (1, 2):
        lengths.add(8)
        for byte_count in range(1, min(252, max_len - 4)):
            candidate = 5 + byte_count
            if candidate <= max_len:
                lengths.add(candidate)
    elif fc in (3, 4):
        lengths.add(8)
        max_bc = min(2 * 125, max_len - 5)
        for byte_count in range(2, max_bc + 1, 2):
            candidate = 5 + byte_count
            if candidate <= max_len:
                lengths.add(candidate)
    elif fc in (5, 6):
        lengths.add(8)
    elif fc == 15:
        lengths.add(8)
        max_bc = min(246, max_len - 9)
        for byte_count in range(1, max_bc + 1):
            candidate = 9 + byte_count
            if candidate <= max_len:
                lengths.add(candidate)
    elif fc == 16:
        lengths.add(8)
        max_bc = min(2 * 123, max_len - 9)
        for byte_count in range(2, max_bc + 1, 2):
            candidate = 9 + byte_count
            if candidate <= max_len:
                lengths.add(candidate)
    elif function_code & 0x80:
        lengths.add(5)

    if not lengths:
        lengths.update(range(4, max_len + 1))

    return sorted(length for length in lengths if 4 <= length <= max_len)


def split_modbus_frames(data: bytes) -> Tuple[List[bytes], bytes]:
    """Divide un pacchetto grezzo in frame Modbus validi e resto non riconosciuto."""
    frames: List[bytes] = []
    idx = 0
    n = len(data)
    while idx + 4 <= n:
        func = data[idx + 1] if idx + 1 < n else None
        max_len = min(256, n - idx)
        candidates = _candidate_frame_lengths(func, max_len)
        found = False
        for length in candidates:
            if idx + length > n:
                continue
            frame = data[idx : idx + length]
            if len(frame) < 4:
                continue
            calc_crc = compute_crc(frame[:-2])
            frame_crc = int.from_bytes(frame[-2:], "little")
            if calc_crc == frame_crc:
                frames.append(frame)
                idx += length
                found = True
                break
        if not found:
            idx += 1
    leftover = data[idx:]
    return frames, leftover


def parse_fc03_request(frame: bytes) -> Optional[Tuple[int, int, int]]:
    """Estrae (unit_id, start_addr, quantity) da una richiesta FC03."""
    if len(frame) < 8:
        return None
    unit = frame[0]
    func = frame[1]
    if func & 0x7F != 3 or func & 0x80:
        return None
    payload = frame[2:-2]
    if len(payload) != 4:
        return None
    start_addr = int.from_bytes(payload[0:2], "big")
    quantity = int.from_bytes(payload[2:4], "big")
    return unit, start_addr, quantity


def parse_fc03_response(frame: bytes) -> Optional[Tuple[int, Sequence[int]]]:
    """Estrae (unit_id, lista valori) da una risposta FC03."""
    if len(frame) < 5:
        return None
    unit = frame[0]
    func = frame[1]
    if func & 0x7F != 3 or func & 0x80:
        return None
    payload = frame[2:-2]
    if not payload:
        return None
    byte_count = payload[0]
    data_bytes = payload[1 : 1 + byte_count]
    values: List[int] = []
    for idx in range(0, len(data_bytes), 2):
        chunk = data_bytes[idx : idx + 2]
        if len(chunk) == 2:
            values.append(int.from_bytes(chunk, "big"))
    if not values:
        return None
    return unit, values


def parse_fc06(frame: bytes) -> Optional[Tuple[int, int, int]]:
    """Estrae (unit_id, register, value) da un frame FC06 valido."""
    if len(frame) < 8:
        return None
    unit = frame[0]
    func = frame[1]
    if func & 0x7F != 6:
        return None
    payload = frame[2:-2]
    if len(payload) < 4:
        return None
    register = int.from_bytes(payload[0:2], "big")
    value = int.from_bytes(payload[2:4], "big")
    return unit, register, value


def parse_fc16_request(frame: bytes) -> Optional[Tuple[int, int, Sequence[int]]]:
    """Estrae (unit_id, start_addr, valori) da una richiesta FC16."""
    if len(frame) < 9:
        return None
    unit = frame[0]
    func = frame[1]
    if func & 0x7F != 16 or func & 0x80:
        return None
    payload = frame[2:-2]
    if len(payload) < 5:
        return None
    start_addr = int.from_bytes(payload[0:2], "big")
    quantity = int.from_bytes(payload[2:4], "big")
    byte_count = payload[4]
    data_bytes = payload[5 : 5 + byte_count]
    values: List[int] = []
    for idx in range(0, len(data_bytes), 2):
        chunk = data_bytes[idx : idx + 2]
        if len(chunk) == 2:
            values.append(int.from_bytes(chunk, "big"))
    if not values:
        return None
    if 0 < quantity < len(values):
        values = values[:quantity]
    return unit, start_addr, values


def iter_frames(frames: Iterable[bytes]) -> Iterator[bytes]:
    """Iteratore che filtra i frame Modbus RTU validi."""
    for frame in frames:
        if len(frame) >= 4 and compute_crc(frame[:-2]) == int.from_bytes(frame[-2:], "little"):
            yield frame
