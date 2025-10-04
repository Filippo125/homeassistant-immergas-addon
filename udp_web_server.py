"""Web dashboard per visualizzare pacchetti UDP in tempo reale.

Esegue un listener UDP e pubblica i messaggi ricevuti via Server-Sent Events.
"""
from __future__ import annotations

import argparse
import html
import json
import socket
import threading
import time
from collections import Counter, deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import SimpleQueue
from pathlib import Path
from typing import Deque, Iterable, List, Optional, Tuple
from datetime import datetime
from urllib.parse import parse_qs, urlparse
from itertools import count

UDP_BUFFER_SIZE = 2048
MESSAGE_HISTORY = 2

subscribers: set[SimpleQueue[dict]] = set()
subscribers_lock = threading.Lock()
message_history: Deque[dict] = deque(maxlen=MESSAGE_HISTORY)
packet_log_lock = threading.Lock()
PACKET_LOG_PATH: Optional[Path] = None

sequence_counter = count(1)
sequence_lock = threading.Lock()


def next_sequence() -> int:
    with sequence_lock:
        return next(sequence_counter)

MODBUS_FUNCTION_NAMES = {
    1: "Read Coils",
    2: "Read Discrete Inputs",
    3: "Read Holding Registers",
    4: "Read Input Registers",
    5: "Write Single Coil",
    6: "Write Single Register",
    15: "Write Multiple Coils",
    16: "Write Multiple Registers",
}

MODBUS_EXCEPTION_CODES = {
    1: "Funzione non supportata",
    2: "Indirizzo dati errato",
    3: "Valore dati non valido",
    4: "Errore del dispositivo slave",
    5: "Acknowledge",
    6: "Slave busy",
    8: "Parity error",
    10: "Gate path unavailable",
    11: "Target device fail to respond",
}


def format_bytes(data: bytes) -> str:
    return data.hex(" ").upper() if data else ""


def split_byte(value: Optional[int]) -> Tuple[str, str]:
    if value is None:
        return "", ""
    return str(value), f"0x{value:02X}"


def make_field(label: str, value: int, size: int = 2) -> dict:
    width = size * 2
    return {
        "label": label,
        "hex": f"0x{value:0{width}X}",
        "dec": value,
    }


def make_raw_field(label: str, data: bytes) -> dict:
    return {
        "label": label,
        "hex": format_bytes(data),
        "dec": None,
    }


def compute_crc(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def append_packet_log(timestamp: str, data: bytes) -> None:
    if PACKET_LOG_PATH is None:
        return
    hex_payload = format_bytes(data)
    line = f"{timestamp},{hex_payload}\n"
    try:
        with packet_log_lock:
            with PACKET_LOG_PATH.open("a", encoding="ascii", errors="ignore") as fh:
                fh.write(line)
                fh.flush()
    except OSError as exc:
        print(f"Impossibile scrivere sul file di log {PACKET_LOG_PATH}: {exc}")


def read_packet_log(max_lines: Optional[int] = None) -> List[Tuple[str, bytes]]:
    if PACKET_LOG_PATH is None or not PACKET_LOG_PATH.exists():
        return []
    if max_lines is not None and max_lines <= 0:
        return []

    raw_lines: List[str]
    with packet_log_lock:
        try:
            with PACKET_LOG_PATH.open("r", encoding="ascii", errors="ignore") as fh:
                raw_lines = fh.read().splitlines()
        except OSError as exc:
            print(f"Impossibile leggere il file di log {PACKET_LOG_PATH}: {exc}")
            return []

    if max_lines is not None:
        raw_lines = raw_lines[-max_lines:]

    entries: List[Tuple[str, bytes]] = []
    for line in raw_lines:
        if not line:
            continue
        if "," not in line:
            continue
        timestamp, hex_payload = line.split(",", 1)
        payload_clean = "".join(hex_payload.split())
        if not payload_clean:
            entries.append((timestamp, b""))
            continue
        try:
            data = bytes.fromhex(payload_clean)
        except ValueError:
            continue
        entries.append((timestamp, data))
    return entries


def extract_fc03_reads(entries: Iterable[Tuple[str, bytes]]) -> List[dict]:
    rows: List[dict] = []
    for timestamp, data in entries:
        if not data:
            continue
        frames, _ = split_modbus_frames(data)
        frame_list = frames if frames else [data]
        pending_request: Optional[tuple[int, int]] = None
        for frame in frame_list:
            if len(frame) < 3:
                continue
            func = frame[1]
            fc = func & 0x7F
            is_exception = func & 0x80 == 0x80
            if fc != 3 or is_exception:
                continue

            payload = frame[2:-2] if len(frame) >= 4 else frame[2:]
            if len(payload) == 4:
                start_addr = int.from_bytes(payload[0:2], "big")
                quantity = int.from_bytes(payload[2:4], "big")
                pending_request = (start_addr, quantity)
                continue

            if not payload:
                continue

            byte_count = payload[0]
            data_bytes = payload[1 : 1 + byte_count]
            register_values: List[int] = []
            for idx in range(0, len(data_bytes), 2):
                chunk = data_bytes[idx : idx + 2]
                if len(chunk) == 2:
                    register_values.append(int.from_bytes(chunk, "big"))

            if not register_values:
                continue

            if pending_request:
                start_addr, _ = pending_request
                for offset, value in enumerate(register_values):
                    addr = start_addr + offset
                    rows.append(
                        {
                            "timestamp": timestamp,
                            "address_dec": addr,
                            "address_hex": f"0x{addr:04X}",
                            "value_dec": value,
                            "value_hex": f"0x{value:04X}",
                        }
                    )
            else:
                for offset, value in enumerate(register_values):
                    rows.append(
                        {
                            "timestamp": timestamp,
                            "address_dec": offset,
                            "address_hex": f"Reg {offset}",
                            "value_dec": value,
                            "value_hex": f"0x{value:04X}",
                        }
                    )
            pending_request = None
    return rows


def extract_fc06_writes(entries: Iterable[Tuple[str, bytes]]) -> List[dict]:
    rows: List[dict] = []
    for timestamp, data in entries:
        if not data:
            continue
        frames, _ = split_modbus_frames(data)
        frame_list = frames if frames else [data]
        pending: Optional[Tuple[int, int]] = None
        for frame in frame_list:
            if len(frame) < 3:
                continue
            func = frame[1]
            fc = func & 0x7F
            is_exception = func & 0x80 == 0x80
            if fc != 6 or is_exception:
                continue

            payload = frame[2:-2] if len(frame) >= 4 else frame[2:]
            if len(payload) < 4:
                continue
            register = int.from_bytes(payload[0:2], "big")
            value = int.from_bytes(payload[2:4], "big")

            if pending and pending == (register, value):
                direction = "response"
                pending = None
            else:
                direction = "request"
                pending = (register, value)

            rows.append(
                {
                    "timestamp": timestamp,
                    "register_dec": register,
                    "register_hex": f"0x{register:04X}",
                    "value_dec": value,
                    "value_hex": f"0x{value:04X}",
                    "direction": direction,
                }
            )
    return rows


def parse_address_value(raw: str) -> Optional[int]:
    if not raw:
        return None
    try:
        base = 16 if raw.lower().startswith("0x") else 10
        return int(raw, base)
    except ValueError:
        return None


def parse_datetime_value(raw: str) -> Tuple[Optional[datetime], Optional[str]]:
    if not raw:
        return None, None
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S"), None
    except ValueError:
        return None, f"Formato data/ora non valido: '{raw}' (usa YYYY-MM-DD HH:MM:SS)"


def prepare_history_filters(query: dict[str, list[str]]) -> dict:
    def first(name: str) -> str:
        values = query.get(name)
        if not values:
            return ""
        return values[0].strip()

    start_raw = first("start")
    end_raw = first("end")
    start_ts_raw = first("start_ts")
    end_ts_raw = first("end_ts")

    messages: List[str] = []

    start_addr = parse_address_value(start_raw)
    end_addr = parse_address_value(end_raw)
    if start_addr is not None and end_addr is not None and end_addr < start_addr:
        start_addr, end_addr = end_addr, start_addr
        messages.append("Intervallo indirizzi invertito: limiti scambiati.")

    start_time, start_time_error = parse_datetime_value(start_ts_raw)
    end_time, end_time_error = parse_datetime_value(end_ts_raw)
    if start_time_error:
        messages.append(start_time_error)
    if end_time_error:
        messages.append(end_time_error)
    if start_time is not None and end_time is not None and end_time < start_time:
        start_time, end_time = end_time, start_time
        messages.append("Intervallo temporale invertito: limiti scambiati.")

    return {
        "start_addr": start_addr,
        "end_addr": end_addr,
        "start_time": start_time,
        "end_time": end_time,
        "start_raw": start_raw,
        "end_raw": end_raw,
        "start_ts_raw": start_ts_raw,
        "end_ts_raw": end_ts_raw,
        "messages": messages,
    }


def _candidate_frame_lengths(func: Optional[int], max_len: int) -> List[int]:
    lengths: set[int] = set()
    if func is None:
        return list(range(4, max_len + 1))

    if func in (1, 2):
        lengths.add(8)
        for byte_count in range(1, min(252, max_len - 4)):
            candidate = 5 + byte_count
            if candidate <= max_len:
                lengths.add(candidate)
    elif func in (3, 4):
        lengths.add(8)
        max_bc = min(2 * 125, max_len - 5)
        for byte_count in range(2, max_bc + 1, 2):
            candidate = 5 + byte_count
            if candidate <= max_len:
                lengths.add(candidate)
    elif func in (5, 6):
        lengths.add(8)
    elif func == 15:
        lengths.add(8)
        max_bc = min(246, max_len - 9)
        for byte_count in range(1, max_bc + 1):
            candidate = 9 + byte_count
            if candidate <= max_len:
                lengths.add(candidate)
    elif func == 16:
        lengths.add(8)
        max_bc = min(2 * 123, max_len - 9)
        for byte_count in range(2, max_bc + 1, 2):
            candidate = 9 + byte_count
            if candidate <= max_len:
                lengths.add(candidate)
    elif func & 0x80:
        lengths.add(5)

    if not lengths:
        lengths.update(range(4, max_len + 1))

    return sorted(length for length in lengths if 4 <= length <= max_len)


def split_modbus_frames(data: bytes) -> Tuple[List[bytes], bytes]:
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


def extract_coils(data: bytes, quantity: Optional[int] = None) -> List[int]:
    coils: List[int] = []
    for byte in data:
        for bit_idx in range(8):
            coils.append((byte >> bit_idx) & 0x01)
            if quantity is not None and len(coils) >= quantity:
                return coils
    return coils


def decode_modbus_payload(function_code: Optional[int], payload: bytes) -> dict:
    fields: List[dict] = []
    notes: List[str] = []
    summary = ""
    frame_type = "unknown"
    function_label: Optional[str] = None

    if function_code is None:
        if payload:
            fields.append(make_raw_field("Payload", payload))
        return {
            "fields": fields,
            "notes": notes,
            "summary": summary,
            "function_label": function_label,
            "frame_type": frame_type,
            "is_exception": False,
        }

    fc = function_code & 0x7F
    is_exception = function_code & 0x80 == 0x80
    length = len(payload)
    function_label = MODBUS_FUNCTION_NAMES.get(fc)

    if is_exception:
        summary = f"Eccezione {function_label or f'funzione 0x{fc:02X}'}"
        if payload:
            code = payload[0]
            description = MODBUS_EXCEPTION_CODES.get(code, "Codice eccezione sconosciuto")
            fields.append(make_field("Exception Code", code, size=1))
            notes.append(description)
            if len(payload) > 1:
                fields.append(make_raw_field("Dati extra", payload[1:]))
        else:
            notes.append("Nessun codice eccezione fornito")
        return {
            "fields": fields,
            "notes": notes,
            "summary": summary,
            "function_label": function_label,
            "frame_type": "exception",
            "is_exception": True,
        }

    summary_prefix = function_label or f"Funzione 0x{fc:02X}"

    if fc in (1, 2, 3, 4):
        if length != 4 and length >= 1:
            byte_count = payload[0]
            fields.append(make_field("Byte Count", byte_count, size=1))
            data_bytes = payload[1:]
            if byte_count != len(data_bytes):
                notes.append("Byte count non coerente con la lunghezza dei dati.")
            if byte_count > len(data_bytes):
                byte_count = len(data_bytes)
            data_portion = data_bytes[:byte_count]
            extra = data_bytes[byte_count:]
            frame_type = "response"
            summary = f"Risposta {summary_prefix}: {byte_count} byte dati"
            if fc in (3, 4):
                register_values: List[int] = []
                for idx in range(0, len(data_portion), 2):
                    chunk = data_portion[idx : idx + 2]
                    if len(chunk) == 2:
                        value = int.from_bytes(chunk, "big")
                        register_values.append(value)
                        fields.append(make_field(f"Registro {idx // 2 + 1}", value))
                    elif chunk:
                        fields.append(make_raw_field(f"Dato incompleto {idx // 2 + 1}", chunk))
                if register_values:
                    quantity = len(register_values)
                    notes.append(
                        "Valori registri: "
                        + ", ".join(str(val) for val in register_values[:12])
                        + ("…" if len(register_values) > 12 else "")
                    )
                    notes.append(f"Registri letti: {quantity}")
            else:
                if data_portion:
                    coils = extract_coils(data_portion, None)
                    on_count = sum(coils)
                    total = len(coils)
                    fields.append(make_raw_field("Coils/Data", data_portion))
                    preview = ", ".join(
                        f"{idx}:{'ON' if state else 'OFF'}" for idx, state in enumerate(coils[:16])
                    )
                    if preview:
                        notes.append(
                            f"Coil attive: {on_count}/{total}" + (f" — {preview}" if preview else "")
                        )
                if extra:
                    fields.append(make_raw_field("Dati extra", extra))
        elif length >= 4:
            start_addr = int.from_bytes(payload[0:2], "big")
            quantity = int.from_bytes(payload[2:4], "big")
            fields.append(make_field("Start Address", start_addr))
            fields.append(make_field("Quantity", quantity))
            frame_type = "request"
            summary = f"Richiesta {summary_prefix}: start {start_addr}, qty {quantity}"
            extra = payload[4:]
            if extra:
                fields.append(make_raw_field("Dati aggiuntivi", extra))
                notes.append("Sono presenti byte aggiuntivi oltre ai campi standard della richiesta.")
        elif payload:
            fields.append(make_raw_field("Payload", payload))
            summary = f"{summary_prefix}: dati grezzi ({length} byte)"
    elif fc in (5, 6):
        if length >= 4:
            address = int.from_bytes(payload[0:2], "big")
            value = int.from_bytes(payload[2:4], "big")
            fields.append(make_field("Address", address))
            fields.append(make_field("Value", value))
            frame_type = "request/response"
            if fc == 5:
                if value == 0xFF00:
                    status = "ON"
                elif value == 0x0000:
                    status = "OFF"
                else:
                    status = f"valore 0x{value:04X}"
                summary = f"{summary_prefix}: coil {address} -> {status}"
            else:
                summary = f"{summary_prefix}: registro {address} = {value}"
        elif payload:
            fields.append(make_raw_field("Payload", payload))
    elif fc == 15:
        if length == 4:
            start_addr = int.from_bytes(payload[0:2], "big")
            quantity = int.from_bytes(payload[2:4], "big")
            fields.append(make_field("Start Address", start_addr))
            fields.append(make_field("Quantity", quantity))
            frame_type = "response"
            summary = f"Risposta {summary_prefix}: start {start_addr}, qty {quantity}"
        elif length >= 5:
            start_addr = int.from_bytes(payload[0:2], "big")
            quantity = int.from_bytes(payload[2:4], "big")
            byte_count = payload[4]
            fields.append(make_field("Start Address", start_addr))
            fields.append(make_field("Quantity", quantity))
            fields.append(make_field("Byte Count", byte_count, size=1))
            values = payload[5 : 5 + byte_count]
            extra = payload[5 + byte_count :]
            frame_type = "request"
            summary = (
                f"Richiesta {summary_prefix}: start {start_addr}, qty {quantity}, "
                f"{byte_count} byte"
            )
            if values:
                fields.append(make_raw_field("Values", values))
                coils = extract_coils(values, quantity)
                if coils:
                    notes.append(
                        "Valori coil: "
                        + ", ".join(
                            f"{start_addr + idx}:{'ON' if state else 'OFF'}"
                            for idx, state in enumerate(coils[:16])
                        )
                        + ("…" if len(coils) > 16 else "")
                    )
            if len(values) < byte_count:
                notes.append("Byte count maggiore dei dati disponibili.")
            if extra:
                fields.append(make_raw_field("Dati extra", extra))
        elif payload:
            fields.append(make_raw_field("Payload", payload))
    elif fc == 16:
        if length == 4:
            start_addr = int.from_bytes(payload[0:2], "big")
            quantity = int.from_bytes(payload[2:4], "big")
            fields.append(make_field("Start Address", start_addr))
            fields.append(make_field("Quantity", quantity))
            frame_type = "response"
            summary = f"Risposta {summary_prefix}: start {start_addr}, qty {quantity}"
        elif length >= 5:
            start_addr = int.from_bytes(payload[0:2], "big")
            quantity = int.from_bytes(payload[2:4], "big")
            byte_count = payload[4]
            fields.append(make_field("Start Address", start_addr))
            fields.append(make_field("Quantity", quantity))
            fields.append(make_field("Byte Count", byte_count, size=1))
            values = payload[5 : 5 + byte_count]
            extra = payload[5 + byte_count :]
            frame_type = "request"
            summary = (
                f"Richiesta {summary_prefix}: start {start_addr}, qty {quantity}, "
                f"{byte_count} byte"
            )
            for idx in range(0, len(values), 2):
                chunk = values[idx : idx + 2]
                if len(chunk) == 2:
                    reg_value = int.from_bytes(chunk, "big")
                    fields.append(make_field(f"Registro {idx // 2}", reg_value))
                elif chunk:
                    fields.append(make_raw_field(f"Dato incompleto {idx // 2}", chunk))
            if len(values) < byte_count:
                notes.append("Byte count maggiore dei dati disponibili.")
            if extra:
                fields.append(make_raw_field("Dati extra", extra))
        elif payload:
            fields.append(make_raw_field("Payload", payload))
    else:
        if payload:
            fields.append(make_raw_field("Payload", payload))
            summary = f"{summary_prefix}: {length} byte"

    return {
        "fields": fields,
        "notes": notes,
        "summary": summary,
        "function_label": function_label,
        "frame_type": frame_type,
        "is_exception": False,
    }


class UdpToWebHandler(BaseHTTPRequestHandler):
    """Gestisce la pagina HTML e il flusso SSE."""

    INDEX_HTML = """<!DOCTYPE html>
<html lang=\"it\">
<head>
    <meta charset=\"utf-8\">
    <title>UDP Live Monitor</title>
    <style>
        :root { color-scheme: dark light; }
        body { font-family: system-ui, -apple-system, sans-serif; margin: 0; padding: 1.5rem; background: #111; color: #f8f9fa; }
        h1 { margin-top: 0; }
        .status { margin: 0.5rem 0 1.5rem; font-size: 0.9rem; }
        table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
        th, td { padding: 0.5rem; border-bottom: 1px solid #333; text-align: left; }
        tbody tr:nth-child(odd) { background: rgba(255, 255, 255, 0.03); }
        code { font-family: ui-monospace, SFMono-Regular, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
        .controls { margin-bottom: 1rem; }
        button { background: #2563eb; border: none; color: white; padding: 0.5rem 1rem; border-radius: 0.25rem; cursor: pointer; }
        button:hover { background: #1d4ed8; }
        button:disabled { background: #4b5563; cursor: not-allowed; }
        .link-button { display: inline-block; margin-left: 0.5rem; background: #16a34a; color: white; padding: 0.5rem 1rem; border-radius: 0.25rem; text-decoration: none; }
        .link-button:hover { background: #15803d; }
        .note { font-size: 0.8rem; color: #f59e0b; }
        .summary { font-weight: 600; margin-bottom: 0.25rem; }
        @media (prefers-color-scheme: light) {
            body { background: #fafafa; color: #111; }
            tbody tr:nth-child(odd) { background: rgba(0, 0, 0, 0.03); }
            th, td { border-bottom: 1px solid #ddd; }
        }
    </style>
</head>
<body>
    <h1>UDP Live Monitor</h1>
    <p class=\"status\" id=\"status\">Connessione SSE: <strong>in attesa…</strong></p>
    <div class=\"controls\">
        <button id=\"togglePause\" type=\"button\">Pausa</button>
        <button id=\"downloadLog\" type=\"button\">Scarica log</button>
        <a class=\"link-button\" href=\"history\">Storico FC03</a>
        <a class=\"link-button\" href=\"history-06\">Storico FC06</a>
        <span id=\"totalCount\">Pacchetti ricevuti: 0</span>
    </div>
    <table>
        <thead>
            <tr>
                <th>#</th>
                <th>Timestamp</th>
                <th>Lunghezza</th>
                <th>Indirizzo</th>
                <th>Funzione</th>
                <th>Payload</th>
                <th>Raw (hex)</th>
            </tr>
        </thead>
        <tbody id=\"events\"></tbody>
    </table>
    <script>
        const statusEl = document.getElementById('status');
        const tableBody = document.getElementById('events');
        const totalCount = document.getElementById('totalCount');
        const toggleButton = document.getElementById('togglePause');
        const downloadButton = document.getElementById('downloadLog');

        let paused = false;
        let counter = 0;

        const updateStatus = (text, ok) => {
            statusEl.innerHTML = `Connessione SSE: <strong>${text}</strong>`;
            statusEl.style.color = ok ? '#10b981' : '#ef4444';
        };

        const renderPayload = (payload) => {
            const fields = payload.payload_fields || [];
            const notes = payload.payload_notes || [];
            const summary = payload.payload_summary || '';
            const blocks = [];
            if (summary) {
                blocks.push(`<div class="summary">${summary}</div>`);
            }
            if (fields.length === 0) {
                const raw = payload.pdu || '';
                blocks.push(raw ? `<code>${raw}</code>` : '—');
            } else {
                fields.forEach((field) => {
                    const pieces = [];
                    if (field.hex) {
                        pieces.push(`<code>${field.hex}</code>`);
                    }
                    if (Number.isFinite(field.dec)) {
                        pieces.push(`(${field.dec})`);
                    }
                    blocks.push(`<div><strong>${field.label}:</strong> ${pieces.join(' ')}</div>`);
                });
            }
            notes.forEach((note) => {
                blocks.push(`<div class="note">${note}</div>`);
            });
            return blocks.join('');
        };

        const eventSource = new EventSource('events');

        eventSource.onopen = () => updateStatus('connessa', true);
        eventSource.onerror = () => updateStatus('errore, tentativo di riconnessione…', false);

        eventSource.onmessage = (event) => {
            if (paused) { return; }
            const payload = JSON.parse(event.data);
            counter += 1;
            totalCount.textContent = `Pacchetti ricevuti: ${counter}`;

            const row = document.createElement('tr');
            row.innerHTML = `
                <td>${payload.seq}</td>
                <td><time>${payload.timestamp}</time></td>
                <td>${payload.length}</td>
                <td>${payload.address_hex || '—'}</td>
                <td>${payload.function_hex || '—'}</td>
                <td>${renderPayload(payload)}</td>
                <td>${payload.hex ? `<code>${payload.hex}</code>` : '—'}</td>
            `;
            tableBody.prepend(row);

            const maxRows = 200;
            while (tableBody.children.length > maxRows) {
                tableBody.removeChild(tableBody.lastChild);
            }
        };

        toggleButton.addEventListener('click', () => {
            paused = !paused;
            toggleButton.textContent = paused ? 'Riprendi' : 'Pausa';
            toggleButton.disabled = false;
        });

        if (downloadButton) {
            downloadButton.addEventListener('click', () => {
                window.location.href = 'download-log';
            });
        }
    </script>
</body>
</html>
"""

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in {"/", "/index.html"}:
            self._serve_index()
        elif path == "/events":
            self._serve_events()
        elif path == "/download-log":
            self._serve_log_download()
        elif path == "/history":
            self._serve_history_fc03(query)
        elif path == "/history-06":
            self._serve_history_fc06(query)
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Risorsa non trovata")

    def log_message(self, fmt: str, *args: object) -> None:
        # Riduce il rumore sullo stdout mantenendo solo errori espliciti.
        pass

    def _serve_index(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(self.INDEX_HTML.encode("utf-8"))

    def _serve_events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        queue = SimpleQueue()
        with subscribers_lock:
            subscribers.add(queue)
            history_snapshot = list(message_history)

        try:
            for item in history_snapshot:
                self._emit_event(item)

            while True:
                item = queue.get()
                self._emit_event(item)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            with subscribers_lock:
                subscribers.discard(queue)

    def _emit_event(self, payload: dict) -> None:
        data = json.dumps(payload, separators=(",", ":"))
        message = f"data: {data}\n\n".encode("utf-8")
        self.wfile.write(message)
        self.wfile.flush()

    def _serve_log_download(self) -> None:
        if PACKET_LOG_PATH is None or not PACKET_LOG_PATH.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "File di log non disponibile")
            return

        try:
            with packet_log_lock:
                data = PACKET_LOG_PATH.read_bytes()
        except OSError as exc:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Errore lettura log: {exc}")
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=ascii")
        self.send_header(
            "Content-Disposition",
            f"attachment; filename=\"{PACKET_LOG_PATH.name}\"",
        )
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _serve_history_fc03(self, query: dict[str, list[str]]) -> None:
        entries = read_packet_log()
        all_rows = extract_fc03_reads(reversed(entries))

        filters = prepare_history_filters(query)
        start_addr = filters["start_addr"]
        end_addr = filters["end_addr"]
        start_time = filters["start_time"]
        end_time = filters["end_time"]
        messages = list(filters["messages"])

        rows_with_dt: List[Tuple[dict, Optional[datetime]]] = []
        for row in all_rows:
            dt = None
            try:
                dt = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
            except (ValueError, KeyError):
                dt = None
            rows_with_dt.append((row, dt))

        filtered_rows: List[dict] = []
        for row, dt in rows_with_dt:
            if start_addr is not None and row["address_dec"] < start_addr:
                continue
            if end_addr is not None and row["address_dec"] > end_addr:
                continue
            if start_time is not None:
                if dt is None or dt < start_time:
                    continue
            if end_time is not None:
                if dt is None or dt > end_time:
                    continue
            filtered_rows.append(row)

        address_stats: dict[int, dict[str, object]] = {}
        for row in filtered_rows:
            addr = row["address_dec"]
            entry = address_stats.setdefault(
                addr,
                {
                    "hex": row["address_hex"],
                    "count": 0,
                    "min": row["value_dec"],
                    "max": row["value_dec"],
                },
            )
            entry["count"] = int(entry["count"]) + 1
            entry["min"] = min(int(entry["min"]), row["value_dec"])
            entry["max"] = max(int(entry["max"]), row["value_dec"])

        unique_addresses = sorted(address_stats.items())

        rows = filtered_rows[:1000]

        start_raw = filters["start_raw"]
        end_raw = filters["end_raw"]
        start_ts_raw = filters["start_ts_raw"]
        end_ts_raw = filters["end_ts_raw"]

        if not rows:
            if start_raw or end_raw or start_ts_raw or end_ts_raw:
                messages.append("Nessuna lettura corrisponde ai filtri specificati.")
            else:
                messages.append("Nessuna lettura Modbus FC03 trovata nel log.")

        info_message = " ".join(messages).strip()
        info_block_html = (
            f'<p class="message">{html.escape(info_message)}</p>' if info_message else ""
        )

        lines: List[str] = []
        lines.append("<!DOCTYPE html>")
        lines.append("<html lang=\"it\">")
        lines.append("<head>")
        lines.append("    <meta charset=\"utf-8\">")
        lines.append("    <title>Storico FC03</title>")
        lines.append("    <style>")
        lines.append("        :root { color-scheme: dark light; }")
        lines.append(
            "        body { font-family: system-ui, -apple-system, sans-serif; margin: 0; padding: 1.5rem; background: #111; color: #f8f9fa; }"
        )
        lines.append("        h1 { margin-top: 0; }")
        lines.append(
            "        .filters { margin: 1rem 0; display: flex; gap: 0.75rem; flex-wrap: wrap; align-items: flex-end; }"
        )
        lines.append(
            "        .filters label { display: flex; flex-direction: column; font-size: 0.85rem; gap: 0.35rem; }"
        )
        lines.append(
            "        .filters input { padding: 0.4rem 0.6rem; border-radius: 0.25rem; border: 1px solid #4b5563; background: #111; color: #f8f9fa; }"
        )
        lines.append(
            "        .filters button { padding: 0.45rem 0.9rem; border-radius: 0.25rem; border: none; background: #2563eb; color: white; cursor: pointer; }"
        )
        lines.append("        .filters button:hover { background: #1d4ed8; }")
        lines.append(
            "        details.addr-summary { margin: 0.5rem 0 1rem; border: 1px solid #374151; border-radius: 0.35rem; padding: 0.75rem 1rem; background: rgba(37, 99, 235, 0.08); }"
        )
        lines.append(
            "        details.addr-summary summary { cursor: pointer; font-weight: 600; }"
        )
        lines.append(
            "        details.addr-summary table { margin-top: 0.75rem; width: auto; min-width: 12rem; }"
        )
        lines.append(
            "        details.addr-summary th, details.addr-summary td { text-align: right; padding: 0.3rem 0.6rem; border-bottom: none; }"
        )
        lines.append("        .message { margin: 0.5rem 0 1rem; color: #f59e0b; }")
        lines.append("        table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }")
        lines.append("        th, td { padding: 0.5rem; border-bottom: 1px solid #333; text-align: left; }")
        lines.append("        tbody tr:nth-child(odd) { background: rgba(255, 255, 255, 0.05); }")
        lines.append("        @media (prefers-color-scheme: light) {")
        lines.append("            body { background: #fafafa; color: #111; }")
        lines.append("            tbody tr:nth-child(odd) { background: rgba(0, 0, 0, 0.04); }")
        lines.append("            th, td { border-bottom: 1px solid #ddd; }")
        lines.append(
            "            .filters input { border: 1px solid #cbd5f5; background: #fff; color: #111; }"
        )
        lines.append("        }")
        lines.append("        a { color: #60a5fa; }")
        lines.append("    </style>")
        lines.append("</head>")
        lines.append("<body>")
        lines.append("    <h1>Storico letture funzione 0x03</h1>")
        lines.append("    <p><a href=\"/\">« Torna alla dashboard</a></p>")
        if unique_addresses:
            lines.append(
                "    <details class=\"addr-summary\" open>"
            )
            lines.append(
                f"        <summary>Indirizzi rilevati: {len(unique_addresses)}</summary>"
            )
            lines.append("        <table>")
            lines.append(
                "            <thead><tr><th>Addr</th><th>Hex</th><th>Occorrenze</th><th>Min</th><th>Max</th></tr></thead>"
            )
            lines.append("            <tbody>")
            for addr_dec, stats in unique_addresses:
                addr_hex = stats["hex"] if isinstance(stats["hex"], str) else f"0x{addr_dec:04X}"
                count = int(stats["count"])
                min_val = int(stats["min"])
                max_val = int(stats["max"])
                lines.append(
                    "                <tr>"
                    f"<td>{addr_dec}</td>"
                    f"<td>{html.escape(addr_hex)}</td>"
                    f"<td>{count}</td>"
                    f"<td>{min_val} (0x{min_val:04X})</td>"
                    f"<td>{max_val} (0x{max_val:04X})</td>"
                    "</tr>"
                )
            lines.append("            </tbody>")
            lines.append("        </table>")
            lines.append("    </details>")
        lines.append("    <form class=\"filters\" method=\"get\" action=\"/history\">")
        lines.append("        <label>")
        lines.append(
            f"            Indirizzo minimo\n            <input type=\"text\" name=\"start\" placeholder=\"es. 100 o 0x64\" value=\"{html.escape(start_raw)}\">"
        )
        lines.append("        </label>")
        lines.append("        <label>")
        lines.append(
            f"            Indirizzo massimo\n            <input type=\"text\" name=\"end\" placeholder=\"es. 120 o 0x78\" value=\"{html.escape(end_raw)}\">"
        )
        lines.append("        </label>")
        lines.append("        <label>")
        lines.append(
            f"            Data/ora minima\n            <input type=\"text\" name=\"start_ts\" placeholder=\"YYYY-MM-DD HH:MM:SS\" value=\"{html.escape(start_ts_raw)}\">"
        )
        lines.append("        </label>")
        lines.append("        <label>")
        lines.append(
            f"            Data/ora massima\n            <input type=\"text\" name=\"end_ts\" placeholder=\"YYYY-MM-DD HH:MM:SS\" value=\"{html.escape(end_ts_raw)}\">"
        )
        lines.append("        </label>")
        lines.append("        <button type=\"submit\">Applica filtri</button>")
        lines.append("    </form>")
        if info_block_html:
            lines.append(f"    {info_block_html}")
        lines.append("    <table>")
        lines.append("        <thead>")
        lines.append("            <tr>")
        lines.append("                <th>Timestamp</th>")
        lines.append("                <th>Indirizzo</th>")
        lines.append("                <th>Valore (dec)</th>")
        lines.append("                <th>Valore (hex)</th>")
        lines.append("            </tr>")
        lines.append("        </thead>")
        lines.append("        <tbody>")
        for item in rows:
            address_repr = f"{item['address_dec']} ({item['address_hex']})"
            lines.append("            <tr>")
            lines.append(f"                <td>{html.escape(item['timestamp'])}</td>")
            lines.append(f"                <td>{html.escape(address_repr)}</td>")
            lines.append(f"                <td>{item['value_dec']}</td>")
            lines.append(f"                <td>{html.escape(item['value_hex'])}</td>")
            lines.append("            </tr>")
        if not rows:
            lines.append(
                "            <tr><td colspan=\"4\">Nessuna lettura da mostrare</td></tr>"
            )
        lines.append("        </tbody>")
        lines.append("    </table>")
        lines.append("</body>")
        lines.append("</html>")

        page = "\n".join(lines)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(page.encode("utf-8"))

    def _serve_history_fc06(self, query: dict[str, list[str]]) -> None:
        entries = read_packet_log()
        all_rows = extract_fc06_writes(reversed(entries))

        filters = prepare_history_filters(query)
        start_addr = filters["start_addr"]
        end_addr = filters["end_addr"]
        start_time = filters["start_time"]
        end_time = filters["end_time"]
        messages = list(filters["messages"])

        rows_with_dt: List[Tuple[dict, Optional[datetime]]] = []
        for row in all_rows:
            dt = None
            try:
                dt = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
            except (ValueError, KeyError):
                dt = None
            rows_with_dt.append((row, dt))

        filtered_rows: List[dict] = []
        for row, dt in rows_with_dt:
            if start_addr is not None and row["register_dec"] < start_addr:
                continue
            if end_addr is not None and row["register_dec"] > end_addr:
                continue
            if start_time is not None:
                if dt is None or dt < start_time:
                    continue
            if end_time is not None:
                if dt is None or dt > end_time:
                    continue
            filtered_rows.append(row)

        register_stats: dict[int, dict[str, object]] = {}
        for row in filtered_rows:
            reg = row["register_dec"]
            entry = register_stats.setdefault(
                reg,
                {
                    "hex": row["register_hex"],
                    "count": 0,
                    "min": row["value_dec"],
                    "max": row["value_dec"],
                },
            )
            entry["count"] = int(entry["count"]) + 1
            entry["min"] = min(int(entry["min"]), row["value_dec"])
            entry["max"] = max(int(entry["max"]), row["value_dec"])

        unique_registers = sorted(register_stats.items())

        rows = filtered_rows[:1000]

        start_raw = filters["start_raw"]
        end_raw = filters["end_raw"]
        start_ts_raw = filters["start_ts_raw"]
        end_ts_raw = filters["end_ts_raw"]

        if not rows:
            if start_raw or end_raw or start_ts_raw or end_ts_raw:
                messages.append("Nessuna scrittura corrisponde ai filtri specificati.")
            else:
                messages.append("Nessuna scrittura Modbus FC06 trovata nel log.")

        info_message = " ".join(messages).strip()
        info_block_html = (
            f'<p class="message">{html.escape(info_message)}</p>' if info_message else ""
        )

        lines: List[str] = []
        lines.append("<!DOCTYPE html>")
        lines.append("<html lang=\"it\">")
        lines.append("<head>")
        lines.append("    <meta charset=\"utf-8\">")
        lines.append("    <title>Storico FC06</title>")
        lines.append("    <style>")
        lines.append("        :root { color-scheme: dark light; }")
        lines.append(
            "        body { font-family: system-ui, -apple-system, sans-serif; margin: 0; padding: 1.5rem; background: #111; color: #f8f9fa; }"
        )
        lines.append("        h1 { margin-top: 0; }")
        lines.append(
            "        .filters { margin: 1rem 0; display: flex; gap: 0.75rem; flex-wrap: wrap; align-items: flex-end; }"
        )
        lines.append(
            "        .filters label { display: flex; flex-direction: column; font-size: 0.85rem; gap: 0.35rem; }"
        )
        lines.append(
            "        .filters input { padding: 0.4rem 0.6rem; border-radius: 0.25rem; border: 1px solid #4b5563; background: #111; color: #f8f9fa; }"
        )
        lines.append(
            "        .filters button { padding: 0.45rem 0.9rem; border-radius: 0.25rem; border: none; background: #2563eb; color: white; cursor: pointer; }"
        )
        lines.append("        .filters button:hover { background: #1d4ed8; }")
        lines.append(
            "        details.addr-summary { margin: 0.5rem 0 1rem; border: 1px solid #374151; border-radius: 0.35rem; padding: 0.75rem 1rem; background: rgba(37, 99, 235, 0.08); }"
        )
        lines.append(
            "        details.addr-summary summary { cursor: pointer; font-weight: 600; }"
        )
        lines.append(
            "        details.addr-summary table { margin-top: 0.75rem; width: auto; min-width: 12rem; }"
        )
        lines.append(
            "        details.addr-summary th, details.addr-summary td { text-align: right; padding: 0.3rem 0.6rem; border-bottom: none; }"
        )
        lines.append("        .message { margin: 0.5rem 0 1rem; color: #f59e0b; }")
        lines.append("        table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }")
        lines.append("        th, td { padding: 0.5rem; border-bottom: 1px solid #333; text-align: left; }")
        lines.append("        tbody tr:nth-child(odd) { background: rgba(255, 255, 255, 0.05); }")
        lines.append("        @media (prefers-color-scheme: light) {")
        lines.append("            body { background: #fafafa; color: #111; }")
        lines.append("            tbody tr:nth-child(odd) { background: rgba(0, 0, 0, 0.04); }")
        lines.append("            th, td { border-bottom: 1px solid #ddd; }")
        lines.append(
            "            .filters input { border: 1px solid #cbd5f5; background: #fff; color: #111; }"
        )
        lines.append("        }")
        lines.append("        a { color: #60a5fa; }")
        lines.append("    </style>")
        lines.append("</head>")
        lines.append("<body>")
        lines.append("    <h1>Storico scritture funzione 0x06</h1>")
        lines.append("    <p><a href=\"/\">« Torna alla dashboard</a></p>")
        if unique_registers:
            lines.append("    <details class=\"addr-summary\" open>")
            lines.append(
                f"        <summary>Registri interessati: {len(unique_registers)}</summary>"
            )
            lines.append("        <table>")
            lines.append(
                "            <thead><tr><th>Registro</th><th>Hex</th><th>Scritture</th><th>Min</th><th>Max</th></tr></thead>"
            )
            lines.append("            <tbody>")
            for reg_dec, stats in unique_registers:
                reg_hex = stats["hex"] if isinstance(stats["hex"], str) else f"0x{reg_dec:04X}"
                count = int(stats["count"])
                min_val = int(stats["min"])
                max_val = int(stats["max"])
                lines.append(
                    "                <tr>"
                    f"<td>{reg_dec}</td>"
                    f"<td>{html.escape(reg_hex)}</td>"
                    f"<td>{count}</td>"
                    f"<td>{min_val} (0x{min_val:04X})</td>"
                    f"<td>{max_val} (0x{max_val:04X})</td>"
                    "</tr>"
                )
            lines.append("            </tbody>")
            lines.append("        </table>")
            lines.append("    </details>")
        lines.append("    <form class=\"filters\" method=\"get\" action=\"/history-06\">")
        lines.append("        <label>")
        lines.append(
            f"            Registro minimo\n            <input type=\"text\" name=\"start\" placeholder=\"es. 100 o 0x64\" value=\"{html.escape(start_raw)}\">"
        )
        lines.append("        </label>")
        lines.append("        <label>")
        lines.append(
            f"            Registro massimo\n            <input type=\"text\" name=\"end\" placeholder=\"es. 120 o 0x78\" value=\"{html.escape(end_raw)}\">"
        )
        lines.append("        </label>")
        lines.append("        <label>")
        lines.append(
            f"            Data/ora minima\n            <input type=\"text\" name=\"start_ts\" placeholder=\"YYYY-MM-DD HH:MM:SS\" value=\"{html.escape(start_ts_raw)}\">"
        )
        lines.append("        </label>")
        lines.append("        <label>")
        lines.append(
            f"            Data/ora massima\n            <input type=\"text\" name=\"end_ts\" placeholder=\"YYYY-MM-DD HH:MM:SS\" value=\"{html.escape(end_ts_raw)}\">"
        )
        lines.append("        </label>")
        lines.append("        <button type=\"submit\">Applica filtri</button>")
        lines.append("    </form>")
        if info_block_html:
            lines.append(f"    {info_block_html}")
        lines.append("    <table>")
        lines.append("        <thead>")
        lines.append("            <tr>")
        lines.append("                <th>Timestamp</th>")
        lines.append("                <th>Registro</th>")
        lines.append("                <th>Valore (dec)</th>")
        lines.append("                <th>Valore (hex)</th>")
        lines.append("                <th>Frame</th>")
        lines.append("            </tr>")
        lines.append("        </thead>")
        lines.append("        <tbody>")
        for item in rows:
            reg_repr = f"{item['register_dec']} ({item['register_hex']})"
            lines.append("            <tr>")
            lines.append(f"                <td>{html.escape(item['timestamp'])}</td>")
            lines.append(f"                <td>{html.escape(reg_repr)}</td>")
            lines.append(f"                <td>{item['value_dec']}</td>")
            lines.append(f"                <td>{html.escape(item['value_hex'])}</td>")
            lines.append(f"                <td>{html.escape(item['direction'])}</td>")
            lines.append("            </tr>")
        if not rows:
            lines.append(
                "            <tr><td colspan=\"5\">Nessuna scrittura da mostrare</td></tr>"
            )
        lines.append("        </tbody>")
        lines.append("    </table>")
        lines.append("</body>")
        lines.append("</html>")

        page = "\n".join(lines)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(page.encode("utf-8"))


def broadcast(message: dict) -> None:
    message_history.append(message)
    with subscribers_lock:
        dead: list[SimpleQueue[dict]] = []
        for queue in subscribers:
            try:
                queue.put(message)
            except Exception:
                dead.append(queue)
        for queue in dead:
            subscribers.discard(queue)


def process_incoming_payload(data: bytes, addr: Tuple[str, int]) -> None:
    seq = next_sequence()
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    append_packet_log(timestamp, data)

    frames, leftover = split_modbus_frames(data)
    has_split = bool(frames)
    raw_frames = frames if has_split else [data]

    frame_infos: List[dict] = []
    for idx, frame in enumerate(raw_frames, start=1):
        address_byte: Optional[int] = frame[0] if len(frame) >= 1 else None
        function_byte: Optional[int] = frame[1] if len(frame) > 1 else None
        _, addr_hex = split_byte(address_byte)
        _, func_hex = split_byte(function_byte)

        if len(frame) >= 4:
            crc_bytes = frame[-2:]
            payload_bytes = frame[2:-2]
        else:
            crc_bytes = frame[-2:] if len(frame) >= 2 else b""
            payload_bytes = frame[2:]

        decoded_payload = decode_modbus_payload(function_byte, payload_bytes)
        payload_fields = [field.copy() for field in decoded_payload["fields"]]
        payload_notes = list(decoded_payload["notes"])
        payload_summary = decoded_payload["summary"]

        received_crc: Optional[int]
        calculated_crc: Optional[int]
        crc_ok: Optional[bool]
        if has_split and len(crc_bytes) == 2:
            received_crc = int.from_bytes(crc_bytes, "little")
            calculated_crc = compute_crc(frame[:-2])
            crc_ok = received_crc == calculated_crc
            if not crc_ok:
                payload_notes.append(
                    f"CRC errato: ricevuto 0x{received_crc:04X}, atteso 0x{calculated_crc:04X}"
                )
        elif len(crc_bytes) == 2:
            received_crc = int.from_bytes(crc_bytes, "little")
            calculated_crc = compute_crc(frame[:-2])
            crc_ok = received_crc == calculated_crc
            if not crc_ok:
                payload_notes.append(
                    f"CRC errato: ricevuto 0x{received_crc:04X}, atteso 0x{calculated_crc:04X}"
                )
        else:
            received_crc = None
            calculated_crc = compute_crc(frame) if frame else None
            crc_ok = None

        frame_infos.append(
            {
                "index": idx,
                "length": len(frame),
                "address_hex": addr_hex,
                "function_hex": func_hex,
                "function_label": decoded_payload["function_label"],
                "frame_type": decoded_payload["frame_type"],
                "summary": payload_summary,
                "fields": payload_fields,
                "notes": payload_notes,
                "pdu": format_bytes(payload_bytes),
                "crc": format_bytes(crc_bytes),
                "crc_ok": crc_ok,
                "crc_value": f"0x{received_crc:04X}" if received_crc is not None else "",
                "crc_calc": f"0x{calculated_crc:04X}" if calculated_crc is not None else "",
                "received_crc": received_crc,
                "calculated_crc": calculated_crc,
                "is_exception": decoded_payload["is_exception"],
                "hex": format_bytes(frame),
            }
        )

    if not frame_infos:
        return

    if len(frame_infos) == 1:
        top = frame_infos[0]
        address_hex = top["address_hex"]
        function_hex = top["function_hex"]
        function_label = top["function_label"]
        frame_type = top["frame_type"]
        payload_fields = top["fields"]
        payload_notes = top["notes"]
        payload_summary = top["summary"]
        pdu_value = top["pdu"]
        crc_hex = top["crc"]
        crc_ok_value = True if top["crc_ok"] is None else top["crc_ok"]
        crc_value_str = top["crc_value"]
        crc_calc_str = top["crc_calc"]
        received_crc_int = top["received_crc"]
        calculated_crc_int = top["calculated_crc"]
        is_exception = top["is_exception"]
    else:
        address_hex_list: List[str] = []
        function_hex_list: List[str] = []
        payload_fields = []
        payload_notes = []
        for info in frame_infos:
            if info["address_hex"] and info["address_hex"] not in address_hex_list:
                address_hex_list.append(info["address_hex"])
            if info["function_hex"] and info["function_hex"] not in function_hex_list:
                function_hex_list.append(info["function_hex"])
            prefix = f"F{info['index']}"
            for field in info["fields"]:
                payload_fields.append(
                    {
                        "label": f"{prefix} {field['label']}",
                        "hex": field["hex"],
                        "dec": field.get("dec"),
                    }
                )
            header_parts = [f"Frame {info['index']}"]
            if info["function_label"]:
                header_parts.append(info["function_label"])
            elif info["function_hex"]:
                header_parts.append(f"Funzione {info['function_hex']}")
            header = " – ".join(header_parts)
            if info["summary"]:
                payload_notes.append(f"{header}: {info['summary']}")
            for note in info["notes"]:
                payload_notes.append(f"{header} › {note}")
            if info["crc_ok"] is False and info["crc_value"]:
                payload_notes.append(
                    f"{header} CRC errato (recv={info['crc_value']}, calc={info['crc_calc']})"
                )
        if leftover:
            payload_notes.append(f"Residuo non decodificato: {format_bytes(leftover)}")

        address_hex = " / ".join(address_hex_list)
        function_hex = " / ".join(function_hex_list)
        function_label = None
        frame_type = "multi"
        payload_summary = f"{len(frame_infos)} frame Modbus nello stesso datagramma"
        pdu_value = " | ".join(
            f"F{info['index']}:{info['pdu']}" for info in frame_infos if info["pdu"]
        )
        crc_hex = " | ".join(
            f"F{info['index']}:{info['crc']}" for info in frame_infos if info["crc"]
        )
        crc_status_values = [info["crc_ok"] for info in frame_infos if info["crc_ok"] is not None]
        crc_ok_value = all(crc_status_values) if crc_status_values else True
        crc_value_str = ""
        crc_calc_str = ""
        received_crc_int = None
        calculated_crc_int = None
        is_exception = any(info["is_exception"] for info in frame_infos)

    frames_payload = [
        {
            "index": info["index"],
            "length": info["length"],
            "hex": info["hex"],
            "pdu": info["pdu"],
            "function_hex": info["function_hex"],
            "function_label": info["function_label"],
            "frame_type": info["frame_type"],
            "summary": info["summary"],
            "payload_fields": info["fields"],
            "payload_notes": info["notes"],
            "crc": info["crc"],
            "crc_ok": info["crc_ok"],
            "crc_value": info["crc_value"],
            "crc_calc": info["crc_calc"],
        }
        for info in frame_infos
    ]

    message = {
        "seq": seq,
        "timestamp": timestamp,
        "length": len(data),
        "address_hex": address_hex or "",
        "function_hex": function_hex or "",
        "pdu": pdu_value,
        "crc": crc_hex,
        "hex": format_bytes(data),
        "payload_fields": payload_fields,
        "payload_notes": payload_notes,
        "payload_summary": payload_summary,
        "function_label": function_label,
        "frame_type": frame_type,
        "is_exception": is_exception,
        "crc_ok": crc_ok_value,
        "crc_calc": crc_calc_str,
        "crc_value": crc_value_str,
        "frames": frames_payload,
    }

    pdu_repr = message["pdu"] or ""
    crc_repr = message["crc"] or ""
    addr_repr = message["address_hex"] or "-"
    func_repr = message["function_hex"] or "-"
    if message["function_label"]:
        func_repr = f"{func_repr} ({message['function_label']})"
    fields_repr = "; ".join(
        f"{field['label']}={field['hex']}"
        + (f"({field['dec']})" if field.get("dec") is not None else "")
        for field in payload_fields
    )
    log_line = (
        f"[{timestamp}] {addr[0]}:{addr[1]} seq={seq} len={len(data)} "
        f"addr={addr_repr} func={func_repr} pdu={pdu_repr} crc={crc_repr}"
    )
    if fields_repr:
        log_line = f"{log_line} fields=[{fields_repr}]"
    if payload_notes:
        notes_text = "; ".join(payload_notes)
        log_line = f"{log_line} note={notes_text}"
    if payload_summary:
        log_line = f"{log_line} summary=\"{payload_summary}\""
    if received_crc_int is not None and calculated_crc_int is not None:
        status = "OK" if crc_ok_value else "ERR"
        log_line = (
            f"{log_line} crc_status={status}(recv=0x{received_crc_int:04X},"
            f"calc=0x{calculated_crc_int:04X})"
        )
    print(log_line)
    broadcast(message)


def udp_listener(
    host: str,
    port: int,
    multicast_group: Optional[str] = None,
    multicast_interface: str = "0.0.0.0",
) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass

    bind_host = host
    if multicast_group:
        bind_host = host if host not in {"", "0.0.0.0"} else "0.0.0.0"
    sock.bind((bind_host, port))

    if multicast_group:
        try:
            mreq = socket.inet_aton(multicast_group) + socket.inet_aton(multicast_interface)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            print(
                "Listener UDP in ascolto su "
                f"{bind_host}:{port} (multicast {multicast_group} via {multicast_interface})"
            )
        except OSError as exc:
            raise RuntimeError(
                f"Impossibile iscriversi al gruppo multicast {multicast_group} "
                f"sull'interfaccia {multicast_interface}: {exc}"
            ) from exc
    else:
        print(f"Listener UDP in ascolto su {bind_host}:{port}")

    while True:
        data, addr = sock.recvfrom(UDP_BUFFER_SIZE)
        process_incoming_payload(data, addr)


def tcp_client_listener(
    host: str,
    port: int,
    buffer_size: int,
    reconnect_delay: float = 2.0,
) -> None:
    remote_addr = (host, port)

    while True:
        try:
            sock = socket.create_connection(remote_addr, timeout=10)
        except (OSError, TimeoutError) as exc:
            print(f"Connessione TCP fallita verso {host}:{port}: {exc}. Riprovo tra {reconnect_delay}s...")
            time.sleep(reconnect_delay)
            continue

        with sock:
            try:
                peer = sock.getpeername()
                remote_repr = f"{peer[0]}:{peer[1]}"
            except OSError:
                peer = remote_addr
                remote_repr = f"{host}:{port}"

            print(f"Client TCP connesso a {remote_repr}")
            buffer = bytearray()

            while True:
                try:
                    chunk = sock.recv(buffer_size)
                except (OSError, TimeoutError) as exc:
                    print(f"Errore durante la ricezione dal server TCP {remote_repr}: {exc}")
                    break

                if not chunk:
                    if buffer:
                        process_incoming_payload(bytes(buffer), peer)
                        buffer.clear()
                    print(f"Connessione TCP chiusa da {remote_repr}. Riprovo tra {reconnect_delay}s...")
                    break

                buffer.extend(chunk)

                while buffer:
                    data_bytes = bytes(buffer)
                    frames, leftover = split_modbus_frames(data_bytes)
                    processed_length = len(data_bytes) - len(leftover)

                    if processed_length > 0:
                        payload = data_bytes[:processed_length]
                        process_incoming_payload(payload, peer)
                        buffer = bytearray(leftover)
                        continue

                    if len(buffer) > buffer_size * 4:
                        process_incoming_payload(bytes(buffer), peer)
                        buffer.clear()
                    break

        time.sleep(reconnect_delay)


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Web dashboard per flusso Modbus via UDP/TCP")
    parser.add_argument(
        "--transport",
        choices=("udp", "tcp"),
        default="udp",
        help="Protocollo di acquisizione: 'udp' (default) o 'tcp'.",
    )
    parser.add_argument("--udp-host", default="0.0.0.0", help="Host su cui ascoltare il socket UDP")
    parser.add_argument("--udp-port", type=int, default=7777, help="Porta UDP da monitorare")
    parser.add_argument(
        "--udp-multicast-group",
        default=None,
        help="Indirizzo IPv4 multicast da cui ricevere (es. 239.0.0.1)",
    )
    parser.add_argument(
        "--udp-multicast-interface",
        default="0.0.0.0",
        help="Indirizzo IPv4 dell'interfaccia da usare per il multicast (default qualsiasi)",
    )
    parser.add_argument("--tcp-host", default="127.0.0.1", help="Host del server TCP da contattare")
    parser.add_argument("--tcp-port", type=int, default=7777, help="Porta del server TCP da contattare")
    parser.add_argument(
        "--tcp-reconnect-delay",
        type=float,
        default=2.0,
        help="Secondi da attendere prima di ritentare la connessione TCP",
    )
    parser.add_argument("--http-host", default="0.0.0.0", help="Host per il server HTTP")
    parser.add_argument("--http-port", type=int, default=8080, help="Porta per il server HTTP")
    parser.add_argument(
        "--buffer-size",
        type=int,
        default=UDP_BUFFER_SIZE,
        help="Dimensione massima in byte letta da ciascun socket",
    )
    parser.add_argument(
        "--history",
        type=int,
        default=MESSAGE_HISTORY,
        help="Numero di messaggi recenti mostrati ai nuovi client",
    )
    parser.add_argument(
        "--packet-log",
        default="packets_log.csv",
        help="Percorso del file CSV (timestamp,payload_hex) per memorizzare i pacchetti",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()

    global UDP_BUFFER_SIZE, message_history, PACKET_LOG_PATH
    UDP_BUFFER_SIZE = args.buffer_size

    message_history = deque(maxlen=args.history)

    if args.packet_log:
        packet_path = Path(args.packet_log).expanduser()
        if not packet_path.is_absolute():
            packet_path = Path.cwd() / packet_path
        packet_path.parent.mkdir(parents=True, exist_ok=True)
        with packet_log_lock:
            packet_path.touch(exist_ok=True)
        PACKET_LOG_PATH = packet_path
    else:
        PACKET_LOG_PATH = None

    if args.transport == "udp":
        print("Modalità di acquisizione: UDP")
        listener_target = udp_listener
        listener_args = (
            args.udp_host,
            args.udp_port,
            args.udp_multicast_group,
            args.udp_multicast_interface,
        )
    else:
        print("Modalità di acquisizione: TCP client")
        listener_target = tcp_client_listener
        listener_args = (
            args.tcp_host,
            args.tcp_port,
            args.buffer_size,
            args.tcp_reconnect_delay,
        )

    listener_thread = threading.Thread(
        target=listener_target,
        args=listener_args,
        daemon=True,
        name=f"{args.transport.upper()}-listener",
    )
    listener_thread.start()

    server_address = (args.http_host, args.http_port)
    httpd = ThreadingHTTPServer(server_address, UdpToWebHandler)
    print(f"Server HTTP disponibile su http://{args.http_host}:{args.http_port}")
    print("Premi CTRL+C per terminare.")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nChiusura in corso…")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
