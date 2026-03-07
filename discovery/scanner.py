"""
Interactive PLC Register Scanner - Reverse Engineering Workstation

Four-mode interactive tool for discovering unknown PLC register maps:
  SCAN     - Live register view with cursor navigation, inline tags & suggestions
  CAPTURE  - Snapshot before/after a single action (e.g. press a button)
  SEQUENCE - Record a full machine cycle as a timestamped event log
  MAP      - Discovery progress dashboard with readiness score & action items

Usage:
  python -m discovery.scanner [--host HOST] [--port PORT]
  python -m discovery.scanner --serial /dev/tty.usbserial-1420 [--baudrate 9600] [--device-id 1]
"""

import asyncio
import argparse
import csv
import json
import os
import re
import socket
import sys
import select
import termios
import tty
import time
from datetime import datetime
from enum import Enum, auto
from dataclasses import dataclass, field

import logging
from serial.tools.list_ports import comports
from pymodbus.client import AsyncModbusTcpClient, AsyncModbusSerialClient
import pymodbus

logging.getLogger("pymodbus").setLevel(logging.CRITICAL)

# pymodbus 3.12+ renamed slave= to device_id=
_PV = tuple(int(x) for x in pymodbus.__version__.split(".")[:2])
_UNIT_KW = "device_id" if _PV >= (3, 12) else "slave"


# ── Delta DVP address ranges ───────────────────────────────────────────────

SCAN_RANGES = {
    "hr": [
        {"name": "D0-D10", "start": 0x1000, "count": 11, "fc": 3},
        {"name": "D100-D102", "start": 0x1064, "count": 3, "fc": 3},
    ],
    "coil": [
        {"name": "M0-M21", "start": 0x0800, "count": 22, "fc": 1},
        {"name": "M100-M104", "start": 0x0864, "count": 5, "fc": 1},
        {"name": "Y0-Y5", "start": 0x0500, "count": 6, "fc": 1},
    ],
    "di": [
        {"name": "X0-X7", "start": 0x0400, "count": 8, "fc": 2},
    ],
}

# Build symbol lookup: (fc, addr) -> "D0", "M1", "Y2", "X3" etc.
SYMBOLS = {}
for i in range(11):
    SYMBOLS[(3, 0x1000 + i)] = f"D{i}"
for i in range(3):
    SYMBOLS[(3, 0x1064 + i)] = f"D{100 + i}"
for i in range(22):
    SYMBOLS[(1, 0x0800 + i)] = f"M{i}"
for i in range(5):
    SYMBOLS[(1, 0x0864 + i)] = f"M{100 + i}"
for i in range(6):
    SYMBOLS[(1, 0x0500 + i)] = f"Y{i}"
for i in range(8):
    SYMBOLS[(2, 0x0400 + i)] = f"X{i}"


def symbol(fc, addr):
    return SYMBOLS.get((fc, addr), f"?{fc}:{addr}")


def is_bit_register(fc, addr):
    """M, X, Y are bit registers (toggle infrequently)."""
    return fc in (1, 2)


# ── Flat register list for cursor navigation ──────────────────────────────

REGISTER_LIST = []
for _r in SCAN_RANGES["hr"]:
    for _i in range(_r["count"]):
        REGISTER_LIST.append((3, _r["start"] + _i))
for _r in SCAN_RANGES["coil"]:
    _sym = SYMBOLS.get((1, _r["start"]), "")
    if _sym.startswith("M"):
        for _i in range(_r["count"]):
            REGISTER_LIST.append((1, _r["start"] + _i))
for _r in SCAN_RANGES["coil"]:
    _sym = SYMBOLS.get((1, _r["start"]), "")
    if _sym.startswith("Y"):
        for _i in range(_r["count"]):
            REGISTER_LIST.append((1, _r["start"] + _i))
for _r in SCAN_RANGES["di"]:
    for _i in range(_r["count"]):
        REGISTER_LIST.append((2, _r["start"] + _i))

# Index lookup for cursor positioning
REGISTER_INDEX = {key: idx for idx, key in enumerate(REGISTER_LIST)}


# ── ANSI helpers ────────────────────────────────────────────────────────────

RST = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
YELLOW = "\033[1;33m"
GREEN = "\033[32m"
BRIGHT_GREEN = "\033[1;32m"
RED = "\033[31m"
CYAN = "\033[36m"
BRIGHT_CYAN = "\033[1;36m"
WHITE = "\033[37m"
MAGENTA = "\033[35m"
BG_RED = "\033[41m"
BG_SELECT = "\033[48;5;237m"
INVERSE = "\033[7m"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"


def goto(row, col):
    return f"\033[{row};{col}H"


def clear_screen():
    return "\033[2J\033[H"


def strip_ansi(text):
    return re.sub(r'\033\[[^m]*m', '', text)


def progress_bar(current, total, width=16):
    if total == 0:
        return f"{DIM}{'░' * width}  0/0    0%{RST}"
    filled = int(width * current / total)
    bar = "█" * filled + "░" * (width - filled)
    pct = int(100 * current / total)
    if pct >= 80:
        color = BRIGHT_GREEN
    elif pct >= 50:
        color = YELLOW
    else:
        color = RED
    return f"{color}{bar}{RST}  {current:>2}/{total:<2}  {pct:>3}%"


# ── Capture label presets ───────────────────────────────────────────────────

LABEL_PRESETS = [
    "Pump ON",
    "Pump OFF",
    "Auto Start",
    "Stop",
    "E-Stop",
    "Jog FWD",
    "Jog BACK",
    "Manual Cut",
]


# ── Tag Store ───────────────────────────────────────────────────────────────

class TagStore:
    """Persistent register tag storage. Saves to discovery/tags.json."""

    def __init__(self, path="discovery/tags.json"):
        self.path = path
        self.tags = {}  # "fc,addr" -> {"name", "confidence", "category", "notes"}
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    self.tags = json.load(f)
            except (json.JSONDecodeError, IOError):
                self.tags = {}

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self.tags, f, indent=2)

    def set_tag(self, fc, addr, name, confidence=1, category=""):
        key = f"{fc},{addr}"
        existing = self.tags.get(key, {})
        self.tags[key] = {
            "name": name,
            "confidence": confidence,
            "category": category or existing.get("category", ""),
            "notes": existing.get("notes", ""),
        }
        self.save()

    def set_confidence(self, fc, addr, confidence):
        key = f"{fc},{addr}"
        if key in self.tags:
            self.tags[key]["confidence"] = confidence
            self.save()

    def get(self, fc, addr):
        return self.tags.get(f"{fc},{addr}")

    def is_tagged(self, fc, addr):
        return f"{fc},{addr}" in self.tags

    def display_inline(self, fc, addr, max_width=22):
        tag = self.get(fc, addr)
        if not tag:
            return ""
        name = tag["name"][:max_width]
        conf = tag.get("confidence", 1)
        if conf >= 3:
            return f" {BRIGHT_CYAN}★★★ {name}{RST}"
        elif conf == 2:
            return f" {GREEN}★★☆ {name}{RST}"
        else:
            return f" {DIM}★☆☆ {name}{RST}"

    def count_by_type(self):
        """Return tagged count per register type prefix (D, M, Y, X)."""
        counts = {"D": 0, "M": 0, "Y": 0, "X": 0}
        for key_str in self.tags:
            fc_s, addr_s = key_str.split(",")
            fc, addr = int(fc_s), int(addr_s)
            sym = symbol(fc, addr)
            prefix = sym[0] if sym[0] in counts else None
            if prefix:
                counts[prefix] += 1
        return counts

    def tagged_categories(self):
        cats = set()
        for tag in self.tags.values():
            cat = tag.get("category", "")
            if cat:
                cats.add(cat)
        return cats


# ── Behavior Analyzer ──────────────────────────────────────────────────────

@dataclass
class Suggestion:
    name: str
    reason: str
    confidence: float  # 0.0-1.0
    category: str      # encoder, counter, setpoint, state, analog, command, status, alarm, safety, motor, solenoid, sensor, output, data, unknown


class BehaviorAnalyzer:
    """Infers register purpose from observed behavior patterns."""

    def suggest(self, fc, addr, activity_info, behavior_data, current_val):
        if not activity_info:
            return None

        count = activity_info["count"]
        val_set = activity_info["values_set"]
        val_min = activity_info["min"]
        val_max = activity_info["max"]
        bd = behavior_data or {}

        if fc == 3:
            return self._suggest_d(count, val_set, val_min, val_max, bd, current_val)
        elif fc == 2:
            return self._suggest_x(count, val_set, bd, current_val)
        elif fc == 1:
            if 0x0500 <= addr < 0x0600:
                return self._suggest_y(count, val_set, bd, current_val)
            else:
                return self._suggest_m(count, val_set, bd, current_val)
        return None

    def _suggest_d(self, count, val_set, val_min, val_max, bd, current_val):
        spread = val_max - val_min
        n_values = len(val_set)
        recent = bd.get("recent_values", [])

        # Monotonic ramp = encoder/position
        if len(recent) >= 5:
            vals = [v for _, v in recent[-10:]]
            if all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1)) and spread > 100:
                return Suggestion("Encoder/position", f"Steady ramp {val_min}->{val_max}", 0.8, "encoder")
            if all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1)) and spread > 100:
                return Suggestion("Countdown/position", f"Ramp down {val_max}->{val_min}", 0.7, "encoder")

        # Increments by 1 = piece counter
        if n_values == count + 1 and spread == count and spread > 1:
            return Suggestion("Piece counter", f"Increments by 1 (now {current_val})", 0.75, "counter")

        # Small enum = state machine / mode
        if n_values <= 6 and val_max <= 10:
            vals_str = ",".join(str(v) for v in sorted(val_set))
            return Suggestion("State/mode enum", f"Values: {{{vals_str}}}", 0.7, "state")

        # Rarely changes = setpoint/config
        if count <= 3 and spread > 0:
            return Suggestion("Setpoint/config", f"Rarely changes (val={current_val})", 0.6, "setpoint")

        # High churn + wide range = analog
        if count > 20 and spread > 50:
            return Suggestion("Analog reading", f"Fluctuates {val_min}-{val_max}", 0.6, "analog")

        if count > 5:
            return Suggestion("Active data", f"Changed {count}x, range {val_min}-{val_max}", 0.3, "data")

        return Suggestion("Data register", f"Changed {count}x", 0.2, "data")

    def _suggest_m(self, count, val_set, bd, current_val):
        pulse_starts = bd.get("pulse_starts", [])
        pulse_ends = bd.get("pulse_ends", [])

        # Measure pulse widths
        if pulse_starts and pulse_ends:
            durations = []
            for start in pulse_starts:
                for end in pulse_ends:
                    if end > start:
                        durations.append(end - start)
                        break
            if durations:
                avg_ms = (sum(durations) / len(durations)) * 1000
                if avg_ms < 500:
                    return Suggestion("Command bit (HMI)", f"Brief pulse ~{avg_ms:.0f}ms", 0.8, "command")
                elif avg_ms > 5000:
                    return Suggestion("Status flag", f"Sustained ON ~{avg_ms / 1000:.1f}s", 0.7, "status")

        # Latching: went ON, never came OFF
        if current_val == 1 and len(pulse_starts) > 0 and len(pulse_ends) == 0:
            return Suggestion("Alarm / latch", "Turned ON, hasn't turned OFF", 0.7, "alarm")

        # Stayed ON with few changes
        if current_val == 1 and count <= 4:
            return Suggestion("Status / mode flag", "Stays ON for long periods", 0.6, "status")

        if count > 10:
            return Suggestion("Busy relay", f"Toggled {count}x — status echo?", 0.5, "status")

        if count > 0:
            return Suggestion("Internal relay", f"Changed {count}x", 0.3, "relay")

        return None

    def _suggest_x(self, count, val_set, bd, current_val):
        # Very high frequency = encoder channel
        if count > 50:
            return Suggestion("Encoder channel", f"Rapid toggling ({count} changes)", 0.85, "encoder")

        # Normally ON / NC safety
        if current_val == 1 and count <= 5:
            return Suggestion("NC safety device", "Normally ON — safety interlock?", 0.7, "safety")

        # Brief activations = limit switch
        pulse_starts = bd.get("pulse_starts", [])
        pulse_ends = bd.get("pulse_ends", [])
        if current_val == 0 and pulse_starts and pulse_ends:
            durations = []
            for start in pulse_starts:
                for end in pulse_ends:
                    if end > start:
                        durations.append(end - start)
                        break
            if durations and (sum(durations) / len(durations)) < 2.0:
                return Suggestion("Limit switch", f"Brief activation ~{sum(durations) / len(durations):.1f}s", 0.7, "sensor")

        if count > 0:
            return Suggestion("Digital sensor", f"Changed {count}x", 0.4, "sensor")

        return None

    def _suggest_y(self, count, val_set, bd, current_val):
        pulse_starts = bd.get("pulse_starts", [])
        pulse_ends = bd.get("pulse_ends", [])

        if pulse_starts and pulse_ends:
            durations = []
            for start in pulse_starts:
                for end in pulse_ends:
                    if end > start:
                        durations.append(end - start)
                        break
            if durations:
                avg = sum(durations) / len(durations)
                if avg < 1.0:
                    return Suggestion("Solenoid / valve", f"Brief pulse ~{avg * 1000:.0f}ms", 0.75, "solenoid")
                elif avg > 5.0:
                    return Suggestion("Motor / pump", f"Sustained ON ~{avg:.1f}s", 0.75, "motor")

        if current_val == 1:
            return Suggestion("Active output", "Currently ON", 0.5, "output")

        if count > 0:
            return Suggestion("Output", f"Toggled {count}x", 0.4, "output")

        return None


# ── Data classes ────────────────────────────────────────────────────────────

class Mode(Enum):
    SCAN = auto()
    CAPTURE = auto()
    SEQUENCE = auto()
    MAP = auto()


class InputState(Enum):
    NORMAL = auto()
    LABEL_MENU = auto()
    LABEL_CUSTOM = auto()
    TAG_INPUT = auto()


@dataclass
class ChangeEvent:
    timestamp: float
    fc: int
    addr: int
    old_val: int
    new_val: int

    @property
    def symbol(self):
        return SYMBOLS.get((self.fc, self.addr), f"?{self.fc}:{self.addr}")

    def to_dict(self):
        return {
            "t": self.timestamp,
            "fc": self.fc,
            "addr": self.addr,
            "symbol": self.symbol,
            "old": self.old_val,
            "new": self.new_val,
        }


@dataclass
class CaptureSession:
    label: str
    start_time: float
    before_snapshot: dict = field(default_factory=dict)
    after_snapshot: dict = field(default_factory=dict)
    changes: list = field(default_factory=list)

    def duration(self):
        if self.changes:
            return self.changes[-1].timestamp - self.start_time
        return 0.0

    def to_dict(self):
        before = {f"{fc},{addr}": val for (fc, addr), val in self.before_snapshot.items()}
        after = {f"{fc},{addr}": val for (fc, addr), val in self.after_snapshot.items()}
        return {
            "label": self.label,
            "start_time": self.start_time,
            "duration_s": self.duration(),
            "before": before,
            "after": after,
            "changes": [c.to_dict() for c in self.changes],
        }


@dataclass
class SequenceSession:
    label: str
    start_time: float
    events: list = field(default_factory=list)
    collapsed_events: list = field(default_factory=list)
    burst_trackers: dict = field(default_factory=dict)
    noise_threshold_ms: int = 500

    def duration(self):
        if self.events:
            return self.events[-1].timestamp - self.start_time
        return time.monotonic() - self.start_time

    def add_event(self, event):
        self.events.append(event)
        key = (event.fc, event.addr)

        if is_bit_register(event.fc, event.addr):
            self.collapsed_events.append(event)
            return

        threshold_s = self.noise_threshold_ms / 1000.0
        if key in self.burst_trackers:
            tracker = self.burst_trackers[key]
            elapsed = event.timestamp - tracker["last_t"]
            if elapsed < threshold_s:
                tracker["last_val"] = event.new_val
                tracker["count"] += 1
                tracker["last_t"] = event.timestamp
                return
            else:
                self._flush_burst(key)

        self.burst_trackers[key] = {
            "first_val": event.old_val,
            "last_val": event.new_val,
            "count": 1,
            "first_t": event.timestamp,
            "last_t": event.timestamp,
            "fc": event.fc,
            "addr": event.addr,
        }
        self.collapsed_events.append(event)

    def _flush_burst(self, key):
        tracker = self.burst_trackers.pop(key, None)
        if tracker and tracker["count"] > 1:
            for i in range(len(self.collapsed_events) - 1, -1, -1):
                e = self.collapsed_events[i]
                if (e.fc, e.addr) == key:
                    burst_event = ChangeEvent(
                        timestamp=tracker["first_t"],
                        fc=tracker["fc"],
                        addr=tracker["addr"],
                        old_val=tracker["first_val"],
                        new_val=tracker["last_val"],
                    )
                    burst_event._burst_count = tracker["count"]
                    burst_event._burst_duration = tracker["last_t"] - tracker["first_t"]
                    self.collapsed_events[i] = burst_event
                    break

    def flush_all_bursts(self):
        for key in list(self.burst_trackers.keys()):
            self._flush_burst(key)

    def to_dict(self):
        self.flush_all_bursts()
        return {
            "label": self.label,
            "start_time": self.start_time,
            "duration_s": self.duration(),
            "noise_threshold_ms": self.noise_threshold_ms,
            "event_count": len(self.events),
            "events": [e.to_dict() for e in self.events],
        }


# ── Register Poller ─────────────────────────────────────────────────────────

class RegisterPoller:
    def __init__(self, client, device_id=1):
        self.client = client
        self.device_id = device_id
        self.values = {}

    async def poll(self):
        changes = []
        now = time.monotonic()
        unit_kw = {_UNIT_KW: self.device_id}

        for _cat, ranges in SCAN_RANGES.items():
            for r in ranges:
                try:
                    if r["fc"] == 3:
                        result = await self.client.read_holding_registers(
                            r["start"], count=r["count"], **unit_kw
                        )
                    elif r["fc"] == 1:
                        result = await self.client.read_coils(
                            r["start"], count=r["count"], **unit_kw
                        )
                    elif r["fc"] == 2:
                        result = await self.client.read_discrete_inputs(
                            r["start"], count=r["count"], **unit_kw
                        )
                    else:
                        continue

                    if result.isError():
                        continue

                    raw = result.registers if r["fc"] == 3 else result.bits[: r["count"]]
                    for i, val in enumerate(raw):
                        addr = r["start"] + i
                        key = (r["fc"], addr)
                        val_int = int(val)
                        if key in self.values:
                            old = self.values[key]
                            if val_int != old:
                                changes.append(ChangeEvent(now, r["fc"], addr, old, val_int))
                        self.values[key] = val_int
                except Exception:
                    pass

        return changes

    def snapshot(self):
        return dict(self.values)


# ── Report Generator ────────────────────────────────────────────────────────

class ReportGenerator:
    @staticmethod
    def generate(session_meta, captures, sequences, activity, tags=None):
        lines = []
        ts = datetime.fromtimestamp(session_meta["start_wall"]).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"# PLC Discovery Report - {ts}")
        lines.append("")
        lines.append(f"## Connection: {session_meta['connection']}")
        dur = session_meta.get("duration_s", 0)
        lines.append(f"## Session duration: {dur:.1f}s | Total scans: {session_meta.get('scan_count', 0)}")
        lines.append("")

        # Tag summary
        if tags:
            lines.append("## Tagged Registers")
            lines.append("")
            lines.append("| Register | Addr | Tag | Confidence | Category |")
            lines.append("|----------|------|-----|------------|----------|")
            for key_str, tag in sorted(tags.items()):
                fc_s, addr_s = key_str.split(",")
                fc, addr = int(fc_s), int(addr_s)
                sym = symbol(fc, addr)
                stars = "*" * tag.get("confidence", 1)
                cat = tag.get("category", "")
                lines.append(f"| {sym} | {addr} | {tag['name']} | {stars} | {cat} |")
            lines.append("")

        # Captures
        if captures:
            lines.append("## Captures")
            lines.append("")
            for i, cap in enumerate(captures, 1):
                t_start = datetime.fromtimestamp(
                    session_meta["start_wall"] + (cap["start_time"] - session_meta["start_mono"])
                ).strftime("%H:%M:%S")
                dur_c = cap.get("duration_s", 0)
                lines.append(f"### {i}. \"{cap['label']}\" ({t_start}, {dur_c:.1f}s)")
                lines.append("")
                if cap["changes"]:
                    lines.append("| Register | Addr | Before | After |")
                    lines.append("|----------|------|--------|-------|")
                    seen = set()
                    for ch in cap["changes"]:
                        key = (ch["fc"], ch["addr"])
                        if key not in seen:
                            seen.add(key)
                            first_old = ch["old"]
                            last_new = ch["new"]
                            for ch2 in cap["changes"]:
                                if (ch2["fc"], ch2["addr"]) == key:
                                    last_new = ch2["new"]
                            lines.append(f"| {ch['symbol']} | {ch['addr']} | {first_old} | {last_new} |")
                else:
                    lines.append("_No changes detected._")
                lines.append("")

        # Sequences
        if sequences:
            lines.append("## Sequences")
            lines.append("")
            for i, seq in enumerate(sequences, 1):
                t_start = datetime.fromtimestamp(
                    session_meta["start_wall"] + (seq["start_time"] - session_meta["start_mono"])
                ).strftime("%H:%M:%S")
                dur_s = seq.get("duration_s", 0)
                lines.append(f"### {i}. \"{seq['label']}\" ({t_start}, {dur_s:.1f}s, {seq['event_count']} events)")
                lines.append("")
                lines.append("| Time | Register | Addr | From | To |")
                lines.append("|------|----------|------|------|----|")
                for ev in seq["events"][:200]:
                    offset = ev["t"] - seq["start_time"]
                    lines.append(f"| +{offset:.3f}s | {ev['symbol']} | {ev['addr']} | {ev['old']} | {ev['new']} |")
                if len(seq["events"]) > 200:
                    lines.append(f"| ... | _{len(seq['events']) - 200} more events_ | | | |")
                lines.append("")

        # Activity summary
        if activity:
            lines.append("## Register Activity Summary")
            lines.append("")
            lines.append("| Register | Addr | Times Changed | Observed Values |")
            lines.append("|----------|------|---------------|-----------------|")
            sorted_activity = sorted(activity.items(), key=lambda x: x[1]["count"], reverse=True)
            for key_str, info in sorted_activity:
                lines.append(
                    f"| {info['symbol']} | {info['addr']} | {info['count']} | {info['values_summary']} |"
                )
            lines.append("")

        return "\n".join(lines)


# ── Display Renderer ────────────────────────────────────────────────────────

class Display:
    def __init__(self):
        self.width = 80
        self.height = 24

    def update_size(self):
        try:
            sz = os.get_terminal_size()
            self.width = max(sz.columns, 60)
            self.height = max(sz.lines, 20)
        except OSError:
            pass

    def _hline(self, left, fill, right):
        return left + fill * (self.width - 2) + right

    def _padline(self, text, side="║"):
        clean = strip_ansi(text)
        pad = self.width - 2 - len(clean)
        if pad < 0:
            pad = 0
        return side + text + " " * pad + side

    def render(self, app):
        self.update_size()
        lines = []

        # Top border
        lines.append(self._hline("╔", "═", "╗"))

        # Header
        conn = f"{GREEN}● Connected{RST}" if app.connected else f"{RED}● Disconnected{RST}"
        lines.append(self._padline(f"  PLC SCANNER | {app.connection_label} | {conn} | Scan #{app.scan_count}"))

        mode_labels = []
        for m in Mode:
            name = m.name
            if m == app.mode:
                mode_labels.append(f"{INVERSE} {name} {RST}")
            else:
                mode_labels.append(f" {DIM}{name}{RST} ")
        mode_str = "  Mode: " + "".join(mode_labels) + f"              Tab=cycle  q=quit"
        lines.append(self._padline(mode_str))

        # Separator
        lines.append(self._hline("╠", "═", "╣"))

        # Calculate available space
        bottom_panel_lines = 4
        if app.mode == Mode.CAPTURE and app.active_capture:
            cap_summary = self._capture_summary(app.active_capture, app.tag_store)
            bottom_panel_lines = max(6, 3 + len(cap_summary))
        elif app.mode == Mode.SEQUENCE and app.active_sequence:
            bottom_panel_lines = max(6, 4 + min(len(app.active_sequence.collapsed_events), 10))
        elif app.input_state in (InputState.LABEL_MENU, InputState.LABEL_CUSTOM, InputState.TAG_INPUT):
            bottom_panel_lines = 4
        elif app.mode == Mode.SCAN:
            bottom_panel_lines = 5
        elif app.mode == Mode.MAP:
            bottom_panel_lines = 3

        reg_lines = self.height - 6 - bottom_panel_lines
        reg_lines = max(reg_lines, 10)

        # Main content area
        if app.mode == Mode.MAP:
            content = self._render_map(app)
        else:
            content = self._render_registers(app)

        for i, line in enumerate(content[:reg_lines]):
            lines.append(self._padline(line))
        for _ in range(reg_lines - min(len(content), reg_lines)):
            lines.append(self._padline(""))

        # Bottom separator
        lines.append(self._hline("╠", "═", "╣"))

        # Bottom panel
        bottom = self._render_bottom(app)
        for line in bottom:
            lines.append(self._padline(line))

        # Bottom border
        lines.append(self._hline("╚", "═", "╝"))

        # Blank trailing rows
        total_rows = len(lines)
        for _ in range(self.height - total_rows):
            lines.append("\033[K")

        return HIDE_CURSOR + "\033[H" + "\n".join(lines)

    def _render_registers(self, app):
        """Render register values with cursor, tags, and suggestions."""
        lines = []
        poller = app.poller
        if not poller or not poller.values:
            lines.append("  Waiting for first scan...")
            return lines

        left_lines = []
        right_lines = []
        selected = app.selected_key

        # ── LEFT COLUMN: D registers ──
        left_lines.append(f"  {BOLD}D REGISTERS (Holding){RST}")
        left_lines.append(f"  {'─' * 30}")
        for r in SCAN_RANGES["hr"]:
            for i in range(r["count"]):
                addr = r["start"] + i
                key = (3, addr)
                val = poller.values.get(key, "?")
                sym = symbol(3, addr)
                ever = "●" if key in app.ever_changed else " "
                recent = f" {YELLOW}◄{RST}" if key in app.change_decay else ""
                tag_str = app.tag_store.display_inline(3, addr, max_width=18)

                # Suggestion hint for untagged active registers
                hint = ""
                if not tag_str and key in app.ever_changed:
                    sug = app.get_suggestion(key)
                    if sug:
                        hint = f" {DIM}? {sug.name}{RST}"

                is_selected = (key == selected and app.mode == Mode.SCAN)

                if is_selected:
                    base = f"  >{ever} {sym:>4} [{addr}]: {val:>6}{recent}{tag_str}{hint}"
                    left_lines.append(f"{BG_SELECT}{base}{RST}")
                elif key in app.change_decay:
                    left_lines.append(f"  {ever} {YELLOW}{sym:>4} [{addr}]: {val:>6}{RST}{recent}{tag_str}{hint}")
                elif key in app.ever_changed:
                    left_lines.append(f"  {CYAN}{ever}{RST} {sym:>4} [{addr}]: {val:>6}{recent}{tag_str}{hint}")
                else:
                    left_lines.append(f"  {DIM}{ever} {sym:>4} [{addr}]: {val:>6}{RST}{recent}{tag_str}{hint}")

        # ── RIGHT COLUMN: M relays ──
        right_lines.append(f"  {GREEN}■{RST}=ON  {DIM}■{RST}=OFF  {YELLOW}■{RST}=changed  {CYAN}■{RST}=tagged")
        right_lines.append(f"  {BOLD}M RELAYS (Coils){RST}")
        right_lines.append(f"  {'─' * 24}")
        m_ranges = [r for r in SCAN_RANGES["coil"] if r["name"].startswith("M")]
        for r in m_ranges:
            row_items = []
            for i in range(r["count"]):
                addr = r["start"] + i
                key = (1, addr)
                val = poller.values.get(key, 0)
                sym = symbol(1, addr)
                is_selected = (key == selected and app.mode == Mode.SCAN)
                tagged = app.tag_store.is_tagged(1, addr)

                if is_selected:
                    row_items.append(f"{BG_SELECT}{BOLD}{sym}={'1' if val else '0'}{RST}")
                elif key in app.change_decay:
                    row_items.append(f"{YELLOW}{sym}={'1' if val else '0'}{RST}")
                elif val:
                    row_items.append(f"{GREEN}{sym}={val}{RST}")
                elif tagged:
                    row_items.append(f"{CYAN}{sym}={val}{RST}")
                else:
                    row_items.append(f"{DIM}{sym}={val}{RST}")
                if len(row_items) == 4:
                    right_lines.append("  " + "  ".join(row_items))
                    row_items = []
            if row_items:
                right_lines.append("  " + "  ".join(row_items))

        # ── Y outputs & X inputs ──
        right_lines.append("")
        right_lines.append(f"  {BOLD}Y OUTPUTS        X INPUTS{RST}")
        right_lines.append(f"  {'─' * 12}       {'─' * 12}")
        y_range = [r for r in SCAN_RANGES["coil"] if r["name"].startswith("Y")][0]
        x_range = SCAN_RANGES["di"][0]
        max_io = max(y_range["count"], x_range["count"])
        for row_i in range(0, max_io, 2):
            parts = "  "
            for j in range(2):
                idx = row_i + j
                if idx < y_range["count"]:
                    addr = y_range["start"] + idx
                    key = (1, addr)
                    val = poller.values.get(key, 0)
                    sym = symbol(1, addr)
                    is_selected = (key == selected and app.mode == Mode.SCAN)
                    tagged = app.tag_store.is_tagged(1, addr)
                    if is_selected:
                        parts += f"{BG_SELECT}{BOLD}{sym}={'1' if val else '0'}{RST} "
                    elif key in app.change_decay:
                        parts += f"{YELLOW}{sym}={'1' if val else '0'}{RST} "
                    elif val:
                        parts += f"{GREEN}{sym}={'1' if val else '0'}{RST} "
                    elif tagged:
                        parts += f"{CYAN}{sym}={'1' if val else '0'}{RST} "
                    else:
                        parts += f"{DIM}{sym}={'1' if val else '0'}{RST} "
                else:
                    parts += "      "
            parts += "       "
            for j in range(2):
                idx = row_i + j
                if idx < x_range["count"]:
                    addr = x_range["start"] + idx
                    key = (2, addr)
                    val = poller.values.get(key, 0)
                    sym = symbol(2, addr)
                    is_selected = (key == selected and app.mode == Mode.SCAN)
                    tagged = app.tag_store.is_tagged(2, addr)
                    if is_selected:
                        parts += f"{BG_SELECT}{BOLD}{sym}={'1' if val else '0'}{RST} "
                    elif key in app.change_decay:
                        parts += f"{YELLOW}{sym}={'1' if val else '0'}{RST} "
                    elif val:
                        parts += f"{GREEN}{sym}={'1' if val else '0'}{RST} "
                    elif tagged:
                        parts += f"{CYAN}{sym}={'1' if val else '0'}{RST} "
                    else:
                        parts += f"{DIM}{sym}={'1' if val else '0'}{RST} "
            right_lines.append(parts)

        # Merge columns
        col_width = (self.width - 4) // 2
        max_lines_count = max(len(left_lines), len(right_lines))
        for i in range(max_lines_count):
            left = left_lines[i] if i < len(left_lines) else ""
            right = right_lines[i] if i < len(right_lines) else ""
            left_clean = strip_ansi(left)
            pad = col_width - len(left_clean)
            if pad < 0:
                pad = 0
            lines.append(left + " " * pad + right)

        return lines

    def _render_map(self, app):
        """Render the MAP mode discovery dashboard."""
        lines = []
        col_width = (self.width - 4) // 2

        # ── Left: Progress ──
        left = []
        left.append(f"  {BOLD}DISCOVERY PROGRESS{RST}")
        left.append(f"  {'─' * 28}")
        left.append("")

        # Count totals per type
        type_info = {"D": [], "M": [], "Y": [], "X": []}
        for key in REGISTER_LIST:
            sym = symbol(*key)
            prefix = sym[0]
            if prefix in type_info:
                type_info[prefix].append(key)

        tagged_counts = app.tag_store.count_by_type()
        total_tagged = 0
        total_regs = len(REGISTER_LIST)
        for prefix, label in [("D", "D Registers"), ("M", "M Relays  "), ("Y", "Y Outputs "), ("X", "X Inputs  ")]:
            total = len(type_info[prefix])
            tagged = tagged_counts.get(prefix, 0)
            total_tagged += tagged
            bar = progress_bar(tagged, total)
            left.append(f"  {label}  {bar}")

        left.append(f"  {'─' * 28}")
        overall_bar = progress_bar(total_tagged, total_regs)
        left.append(f"  {BOLD}Overall     {RST}  {overall_bar}")

        # ── Right: Readiness ──
        right = []
        score = app.readiness_score()
        right.append(f"  {BOLD}SITE READINESS{RST}")
        right.append(f"  {'─' * 28}")
        right.append("")

        # Big readiness gauge
        gauge_width = 24
        filled = int(gauge_width * score / 100)
        gauge_bar = "█" * filled + "░" * (gauge_width - filled)
        if score >= 90:
            gauge_color = BRIGHT_GREEN
            status = f"  {BRIGHT_GREEN}GO — safe to leave site{RST}"
        elif score >= 70:
            gauge_color = YELLOW
            status = f"  {YELLOW}ALMOST — tag remaining items{RST}"
        elif score >= 50:
            gauge_color = YELLOW
            status = f"  {YELLOW}HALFWAY — keep investigating{RST}"
        else:
            gauge_color = RED
            status = f"  {RED}STAY — too many unknowns{RST}"

        right.append(f"  {gauge_color}{gauge_bar}{RST}  {BOLD}{score}%{RST}")
        right.append(status)
        right.append("")

        # Checklist
        checklist = app.build_checklist()
        for label, done in checklist:
            mark = f"{GREEN}✓{RST}" if done else f"{RED}✗{RST}"
            right.append(f"  {mark} {label}")

        # Merge left+right header
        max_hdr = max(len(left), len(right))
        for i in range(max_hdr):
            l = left[i] if i < len(left) else ""
            r = right[i] if i < len(right) else ""
            l_clean = strip_ansi(l)
            pad = col_width - len(l_clean)
            if pad < 0:
                pad = 0
            lines.append(l + " " * pad + r)

        lines.append("")

        # ── Unresolved registers ──
        unresolved = app.get_unresolved()
        if unresolved:
            lines.append(f"  {BOLD}UNRESOLVED{RST}  {DIM}(changed but untagged — these need your attention){RST}")
            lines.append(f"  {'─' * 50}")

            for i, (key, act_info) in enumerate(unresolved[:12]):
                fc, addr = key
                sym_str = symbol(fc, addr)
                count = act_info["count"]
                current = app.poller.values.get(key, "?") if app.poller else "?"
                sug = app.get_suggestion(key)
                is_sel = (i == app.map_cursor and app.mode == Mode.MAP)

                sug_text = ""
                if sug:
                    sug_text = f"  {DIM}? {sug.name} — {sug.reason}{RST}"

                reg_text = (
                    f"  {'>' if is_sel else ' '} {YELLOW}!{RST} "
                    f"{sym_str:>4} [{addr}]  val={current:<6}  changed {count}x{sug_text}"
                )
                if is_sel:
                    lines.append(f"{BG_SELECT}{reg_text}{RST}")
                else:
                    lines.append(reg_text)

            if len(unresolved) > 12:
                lines.append(f"  {DIM}  ... and {len(unresolved) - 12} more{RST}")
        else:
            lines.append(f"  {BRIGHT_GREEN}{BOLD}ALL ACTIVE REGISTERS TAGGED{RST}")
            lines.append("")
            lines.append(f"  {GREEN}You've identified every register that showed activity.{RST}")
            lines.append(f"  {GREEN}Review confidence levels (1-3) and you're good to go.{RST}")

        return lines

    @staticmethod
    def _capture_summary(cap, tag_store=None):
        """Build per-register summary from capture changes, sorted: bits first, then data."""
        reg_map = {}  # (fc, addr) -> {first, last, count, first_time}
        for ch in cap.changes:
            key = (ch.fc, ch.addr)
            if key not in reg_map:
                reg_map[key] = {
                    "first": ch.old_val,
                    "last": ch.new_val,
                    "count": 1,
                    "first_time": ch.timestamp,
                }
            else:
                reg_map[key]["last"] = ch.new_val
                reg_map[key]["count"] += 1

        results = []
        for (fc, addr), info in reg_map.items():
            sym = symbol(fc, addr)
            if fc == 1 and 0x0500 <= addr < 0x0600:
                type_label = "Y out"
            elif fc == 1 and 0x0800 <= addr < 0x0900:
                type_label = "M relay"
            elif fc == 2:
                type_label = "X in"
            elif fc == 3:
                type_label = "D reg"
            else:
                type_label = ""

            tag_name = ""
            if tag_store:
                tag = tag_store.get(fc, addr)
                if tag:
                    tag_name = tag["name"]

            results.append({
                "fc": fc, "addr": addr, "symbol": sym,
                "first": info["first"], "last": info["last"],
                "count": info["count"], "first_time": info["first_time"],
                "type_label": type_label, "tag": tag_name,
            })

        # Sort: bit registers (commands/status) first by time, then data regs
        results.sort(key=lambda r: (0 if r["fc"] in (1, 2) else 1, r["first_time"]))
        return results

    def _render_bottom(self, app):
        """Render mode-dependent bottom panel."""
        lines = []

        if app.input_state == InputState.LABEL_MENU:
            mode_name = "CAPTURE" if app.mode == Mode.CAPTURE else "SEQUENCE"
            lines.append(f"  Enter label for {mode_name}:")
            preset_str = "  "
            for i, label in enumerate(LABEL_PRESETS):
                preset_str += f"{i + 1}={label}  "
            lines.append(preset_str)
            lines.append(f"  c=Custom...  Esc=cancel")
            return lines

        if app.input_state == InputState.LABEL_CUSTOM:
            lines.append(f"  Label: {app.label_buffer}█")
            lines.append(f"  Enter=confirm  Esc=cancel")
            return lines

        if app.input_state == InputState.TAG_INPUT:
            key = app.selected_key
            if key:
                sym_str = symbol(*key)
                addr = key[1]
                lines.append(f"  Tag {sym_str} [{addr}]: {app.label_buffer}█")
            else:
                lines.append(f"  Tag: {app.label_buffer}█")
            sug = app.get_suggestion(app.selected_key) if app.selected_key else None
            hint = f"  {DIM}(suggested: {sug.name}){RST}" if sug else ""
            lines.append(f"  Enter=confirm  Esc=cancel  Backspace=delete{hint}")
            return lines

        if app.mode == Mode.CAPTURE and app.active_capture:
            cap = app.active_capture
            n_changes = len(cap.changes)
            summary = self._capture_summary(cap, app.tag_store)
            n_regs = len(summary)

            lines.append(
                f"  {RED}●{RST} CAPTURE: \"{cap.label}\"  "
                f"{n_changes} events across {n_regs} register{'s' if n_regs != 1 else ''}  "
                f"{DIM}[Enter=finish  Esc=cancel]{RST}"
            )

            if not summary:
                lines.append(f"  {DIM}Waiting for changes...{RST}")
            else:
                # Header
                lines.append(
                    f"  {DIM}{'Register':<8} {'Type':<8} {'First':>6} {'':>3} {'Last':>6}  "
                    f"{'#':>3}  Tag{RST}"
                )
                for info in summary:
                    sym = info["symbol"]
                    fc = info["fc"]
                    first_val = info["first"]
                    last_val = info["last"]
                    count = info["count"]
                    tag_name = info["tag"]
                    addr = info["addr"]

                    # Color by register type
                    if fc in (1, 2) and first_val != last_val:
                        # Bit change — highlight these, they're the important ones
                        on_off = f"{'OFF':>6}  →  {'ON':>6}" if last_val else f"{'ON':>6}  →  {'OFF':>6}"
                        type_label = info["type_label"]
                        color = YELLOW if not tag_name else GREEN
                        lines.append(
                            f"  {color}{sym:<8}{RST} {type_label:<8} {on_off}  {count:>3}x {DIM}{tag_name}{RST}"
                        )
                    elif fc == 3:
                        # Data register — show value change
                        type_label = info["type_label"]
                        if first_val == last_val:
                            arrow = f"{first_val:>6}  =  {last_val:>6}"
                            color = DIM
                        else:
                            arrow = f"{first_val:>6}  →  {last_val:>6}"
                            color = CYAN if count > 5 else ""
                        lines.append(
                            f"  {color}{sym:<8}{RST} {type_label:<8} {arrow}  {count:>3}x {DIM}{tag_name}{RST}"
                        )
                    else:
                        type_label = info["type_label"]
                        on_off = f"{first_val:>6}  →  {last_val:>6}"
                        lines.append(
                            f"  {sym:<8} {type_label:<8} {on_off}  {count:>3}x {DIM}{tag_name}{RST}"
                        )

            while len(lines) < 4:
                lines.append("")
            return lines

        if app.mode == Mode.SEQUENCE and app.active_sequence:
            seq = app.active_sequence
            dur = seq.duration()
            n_events = len(seq.events)
            lines.append(
                f"  {RED}●{RST} SEQUENCE: \"{seq.label}\""
                f"          duration: {dur:.1f}s  "
                f"{DIM}[Enter=stop  Esc=cancel  +/-=threshold:{seq.noise_threshold_ms}ms]{RST}"
            )
            lines.append(f"  Timeline ({n_events} events):")
            recent = seq.collapsed_events[-8:]
            for ev in recent:
                offset = ev.timestamp - seq.start_time
                burst_info = ""
                if hasattr(ev, "_burst_count") and ev._burst_count > 1:
                    burst_info = f"  {DIM}(collapsed {ev._burst_count} changes over {ev._burst_duration:.1f}s){RST}"
                lines.append(
                    f"    +{offset:>7.3f}s  {ev.symbol:>4}  [{ev.addr}]: {ev.old_val} -> {ev.new_val}{burst_info}"
                )
            while len(lines) < 4:
                lines.append("")
            return lines

        if app.mode == Mode.MAP:
            unresolved = app.get_unresolved()
            n_unresolved = len(unresolved)
            score = app.readiness_score()
            lines.append(
                f"  {BOLD}{score}%{RST} ready  |  "
                f"{n_unresolved} unresolved  |  "
                f"↑↓=select  t=tag  Tab=next mode"
            )
            return lines

        # ── SCAN mode footer: selected register inspector ──
        key = app.selected_key
        if key and app.poller:
            fc, addr = key
            sym_str = symbol(fc, addr)
            val = app.poller.values.get(key, "?")
            tag = app.tag_store.get(fc, addr)
            act = app.activity.get(key)

            # Line 1: register info + tag
            if tag:
                conf = tag.get("confidence", 1)
                stars = "★" * conf + "☆" * (3 - conf)
                conf_colors = {1: DIM, 2: GREEN, 3: BRIGHT_CYAN}
                tag_display = f"{conf_colors.get(conf, DIM)}{stars} {tag['name']}{RST}"
                changes_str = f"Changed {act['count']}x" if act else "No changes"
                lines.append(f"  {BOLD}{sym_str}{RST} [{addr}] = {val}  {tag_display}  |  {changes_str}")
            else:
                changes_str = f"Changed {act['count']}x  range: {act['min']}-{act['max']}" if act else "No changes"
                lines.append(f"  {BOLD}{sym_str}{RST} [{addr}] = {val}  {DIM}untagged{RST}  |  {changes_str}")

            # Line 2: suggestion
            sug = app.get_suggestion(key)
            if sug:
                lines.append(f"  {DIM}? {sug.name} — {sug.reason}{RST}")
            else:
                lines.append("")

            # Line 3: stats + keybindings
            n_tagged = sum(1 for k in REGISTER_LIST if app.tag_store.is_tagged(*k))
            score = app.readiness_score()
            cap_count = len(app.captures)
            seq_count = len(app.sequences)
            lines.append(
                f"  Progress: {n_tagged}/{len(REGISTER_LIST)}  "
                f"Readiness: {score}%  "
                f"Cap: {cap_count}  Seq: {seq_count}  |  "
                f"↑↓=nav  t=tag  1-3=conf"
            )
        else:
            lines.append(
                f"  {CYAN}●{RST} = changed this session  "
                f"{YELLOW}◄{RST} = changed in last 2s"
            )
            n_tagged = sum(1 for k in REGISTER_LIST if app.tag_store.is_tagged(*k))
            score = app.readiness_score()
            lines.append(
                f"  Tagged: {n_tagged}/{len(REGISTER_LIST)}  "
                f"Readiness: {score}%  |  "
                f"Captures: {len(app.captures)}  Sequences: {len(app.sequences)}  |  "
                f"Session: {app.session_dir_short}"
            )
            lines.append(f"  {DIM}↑↓=navigate  t=tag  1-3=confidence  Tab=mode{RST}")

        return lines


# ── Main Application ────────────────────────────────────────────────────────

class ScannerApp:
    def __init__(self, host="127.0.0.1", port=5020, serial_port=None, baudrate=9600, device_id=1):
        self.host = host
        self.port = port
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.device_id = device_id
        self.client = None
        self.poller = None
        self.display = Display()

        # State
        self.mode = Mode.SCAN
        self.input_state = InputState.NORMAL
        self.connected = False
        self.scan_count = 0
        self.total_changes = 0
        self.start_mono = time.monotonic()
        self.start_wall = time.time()

        # Change tracking
        self.ever_changed = set()
        self.change_decay = {}

        # Sessions
        self.active_capture = None
        self.active_sequence = None
        self.captures = []
        self.sequences = []

        # Activity tracker
        self.activity = {}

        # Behavior tracking for suggestions
        self.behavior_data = {}  # (fc,addr) -> {recent_values, pulse_starts, pulse_ends, ...}

        # Tag store + analyzer
        self.tag_store = TagStore()
        self.analyzer = BehaviorAnalyzer()
        self._suggestion_cache = {}
        self._suggestion_cache_scan = 0

        # Cursor navigation
        self.cursor_pos = 0
        self.map_cursor = 0

        # Input
        self.label_buffer = ""
        self.pending_mode = None

        # File handles
        self.session_dir = None
        self.session_dir_short = ""
        self.csv_file = None
        self.csv_writer = None
        self.captures_file = None
        self.sequences_file = None

        # Terminal
        self._old_termios = None
        self._running = True

    @property
    def selected_key(self):
        if 0 <= self.cursor_pos < len(REGISTER_LIST):
            return REGISTER_LIST[self.cursor_pos]
        return None

    def readiness_score(self):
        """0-100 score: what % of active registers are tagged, weighted by confidence."""
        if not self.ever_changed:
            return 0
        score = 0.0
        total = len(self.ever_changed)
        for key in self.ever_changed:
            tag = self.tag_store.get(*key)
            if tag:
                conf = tag.get("confidence", 1)
                score += conf / 3.0
        return int((score / total) * 100)

    def build_checklist(self):
        """Return list of (label, is_done) for readiness checklist."""
        items = []
        cats = self.tag_store.tagged_categories()
        active_tagged = sum(1 for k in self.ever_changed if self.tag_store.is_tagged(*k))
        active_total = len(self.ever_changed)

        # Check for key categories
        items.append(("Encoder / position input", "encoder" in cats))
        items.append(("Setpoint registers", "setpoint" in cats or "config" in cats))
        items.append(("Motor / output control", any(c in cats for c in ("motor", "output", "solenoid"))))
        items.append(("Command bits (HMI)", "command" in cats))
        items.append(("Status / feedback flags", "status" in cats or "state" in cats))

        # Y outputs all tagged
        y_keys = [(1, 0x0500 + i) for i in range(6)]
        y_tagged = sum(1 for k in y_keys if self.tag_store.is_tagged(*k))
        items.append((f"Y outputs mapped ({y_tagged}/6)", y_tagged == 6))

        # X inputs all tagged
        x_keys = [(2, 0x0400 + i) for i in range(8)]
        x_tagged = sum(1 for k in x_keys if self.tag_store.is_tagged(*k))
        items.append((f"X inputs mapped ({x_tagged}/8)", x_tagged == 8))

        # All active registers
        items.append((f"All active registers tagged ({active_tagged}/{active_total})", active_tagged >= active_total))

        return items

    def get_unresolved(self):
        """Return list of (key, activity_info) for active but untagged registers, sorted by importance."""
        unresolved = []
        for key in self.ever_changed:
            if not self.tag_store.is_tagged(*key):
                act = self.activity.get(key, {"count": 0})
                unresolved.append((key, act))
        unresolved.sort(key=lambda x: x[1]["count"], reverse=True)
        return unresolved

    def get_suggestion(self, key):
        """Get cached suggestion for a register."""
        if not key:
            return None
        # Refresh cache every 50 scans
        if self.scan_count - self._suggestion_cache_scan > 50:
            self._suggestion_cache.clear()
            self._suggestion_cache_scan = self.scan_count

        if key not in self._suggestion_cache:
            fc, addr = key
            act = self.activity.get(key)
            bd = self.behavior_data.get(key)
            current = self.poller.values.get(key) if self.poller else None
            self._suggestion_cache[key] = self.analyzer.suggest(fc, addr, act, bd, current)
        return self._suggestion_cache[key]

    def _init_session_dir(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_dir = os.path.join("discovery", "sessions", ts)
        self.session_dir_short = f"sessions/{ts}"
        os.makedirs(self.session_dir, exist_ok=True)

        csv_path = os.path.join(self.session_dir, "scan_log.csv")
        self.csv_file = open(csv_path, "w", newline="")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(["timestamp", "scan_num", "fc", "address", "symbol", "old_value", "new_value"])

        self.captures_file = open(os.path.join(self.session_dir, "captures.jsonl"), "a")
        self.sequences_file = open(os.path.join(self.session_dir, "sequences.jsonl"), "a")

    def _log_csv(self, event):
        if self.csv_writer:
            wall_ts = datetime.fromtimestamp(
                self.start_wall + (event.timestamp - self.start_mono)
            ).strftime("%H:%M:%S.%f")[:-3]
            self.csv_writer.writerow([
                wall_ts, self.scan_count, event.fc, event.addr,
                event.symbol, event.old_val, event.new_val,
            ])
            self.csv_file.flush()

    def _save_capture(self, capture):
        data = capture.to_dict()
        data["start_mono"] = capture.start_time
        line = json.dumps(data)
        self.captures_file.write(line + "\n")
        self.captures_file.flush()
        os.fsync(self.captures_file.fileno())

    def _save_sequence(self, sequence):
        data = sequence.to_dict()
        data["start_mono"] = sequence.start_time
        line = json.dumps(data)
        self.sequences_file.write(line + "\n")
        self.sequences_file.flush()
        os.fsync(self.sequences_file.fileno())

    def _save_activity(self):
        path = os.path.join(self.session_dir, "register_activity.json")
        data = {}
        for (fc, addr), info in self.activity.items():
            key_str = f"{fc},{addr}"
            vals = sorted(info["values_set"])
            if len(vals) <= 10:
                summary = ", ".join(str(v) for v in vals)
            else:
                summary = f"{min(vals)}-{max(vals)}"
            data[key_str] = {
                "symbol": symbol(fc, addr),
                "addr": addr,
                "fc": fc,
                "count": info["count"],
                "min": info["min"],
                "max": info["max"],
                "last": info["last"],
                "values_summary": summary,
            }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def _save_report(self):
        meta = {
            "connection": self.connection_label,
            "start_wall": self.start_wall,
            "start_mono": self.start_mono,
            "duration_s": time.monotonic() - self.start_mono,
            "scan_count": self.scan_count,
        }
        cap_dicts = [c.to_dict() for c in self.captures]
        seq_dicts = [s.to_dict() for s in self.sequences]

        act = {}
        for (fc, addr), info in self.activity.items():
            key_str = f"{fc},{addr}"
            vals = sorted(info["values_set"])
            if len(vals) <= 10:
                summary = ", ".join(str(v) for v in vals)
            else:
                summary = f"{min(vals)}-{max(vals)}"
            act[key_str] = {
                "symbol": symbol(fc, addr),
                "addr": addr,
                "count": info["count"],
                "values_summary": summary,
            }

        report_md = ReportGenerator.generate(meta, cap_dicts, seq_dicts, act, tags=self.tag_store.tags)
        path = os.path.join(self.session_dir, "report.md")
        with open(path, "w") as f:
            f.write(report_md)

    def _track_change(self, event):
        key = (event.fc, event.addr)
        self.ever_changed.add(key)
        self.change_decay[key] = time.monotonic() + 2.0
        self.total_changes += 1

        # Activity tracking
        if key not in self.activity:
            self.activity[key] = {
                "count": 0,
                "min": event.new_val,
                "max": event.new_val,
                "last": event.new_val,
                "values_set": {event.old_val, event.new_val},
            }
        info = self.activity[key]
        info["count"] += 1
        info["min"] = min(info["min"], event.new_val)
        info["max"] = max(info["max"], event.new_val)
        info["last"] = event.new_val
        info["values_set"].add(event.new_val)

        # Behavior tracking for suggestions
        if key not in self.behavior_data:
            self.behavior_data[key] = {
                "first_change": event.timestamp,
                "last_change": event.timestamp,
                "recent_values": [],
                "pulse_starts": [],
                "pulse_ends": [],
            }
        bd = self.behavior_data[key]
        bd["last_change"] = event.timestamp
        bd["recent_values"].append((event.timestamp, event.new_val))
        if len(bd["recent_values"]) > 20:
            bd["recent_values"] = bd["recent_values"][-20:]

        # Pulse tracking for bit registers
        if is_bit_register(event.fc, event.addr):
            if event.new_val == 1:
                bd["pulse_starts"].append(event.timestamp)
            else:
                bd["pulse_ends"].append(event.timestamp)
            bd["pulse_starts"] = bd["pulse_starts"][-10:]
            bd["pulse_ends"] = bd["pulse_ends"][-10:]

        # Invalidate suggestion cache for this register
        self._suggestion_cache.pop(key, None)

    def _process_changes(self, changes):
        for event in changes:
            self._log_csv(event)
            self._track_change(event)

            if self.active_capture:
                self.active_capture.changes.append(event)
            if self.active_sequence:
                self.active_sequence.add_event(event)

    def _decay_highlights(self):
        now = time.monotonic()
        expired = [k for k, t in self.change_decay.items() if now >= t]
        for k in expired:
            del self.change_decay[k]

    # ── Keyboard handling ───────────────────────────────────────────────

    def _read_key(self):
        fd = sys.stdin.fileno()
        if not select.select([fd], [], [], 0)[0]:
            return None
        # Read all available bytes at once — captures full escape sequences
        buf = os.read(fd, 64)
        if not buf:
            return None
        b0 = buf[0]
        if b0 == 0x1B:  # ESC
            if len(buf) >= 3 and buf[1] == ord("["):
                code = buf[2]
                if code == ord("A"):
                    return "up"
                if code == ord("B"):
                    return "down"
                return None
            # Only \x1b arrived — wait briefly for rest of sequence
            if len(buf) == 1:
                if select.select([fd], [], [], 0.05)[0]:
                    buf2 = os.read(fd, 64)
                    if len(buf2) >= 2 and buf2[0] == ord("["):
                        if buf2[1] == ord("A"):
                            return "up"
                        if buf2[1] == ord("B"):
                            return "down"
                        return None
                    return None
                return "escape"
            return None
        if b0 == 0x09:
            return "tab"
        if b0 in (0x0D, 0x0A):
            return "enter"
        if b0 == 0x7F:
            return "backspace"
        try:
            return chr(b0)
        except ValueError:
            return None

    def _handle_key(self, key):
        if self.input_state == InputState.LABEL_MENU:
            self._handle_label_menu_key(key)
            return
        if self.input_state == InputState.LABEL_CUSTOM:
            self._handle_label_custom_key(key)
            return
        if self.input_state == InputState.TAG_INPUT:
            self._handle_tag_input_key(key)
            return

        # Normal mode key handling
        if key == "tab":
            self._cycle_mode()
        elif key == "escape":
            self._cancel_active()
        elif key == "enter":
            self._finish_active()
        elif key == "q":
            self._running = False
        elif key == "up":
            self._cursor_up()
        elif key == "down":
            self._cursor_down()
        elif key == "t":
            self._start_tag_input()
        elif key in ("1", "2", "3") and self.mode in (Mode.SCAN, Mode.MAP):
            self._set_confidence(int(key))
        elif key == "+" and self.active_sequence:
            self.active_sequence.noise_threshold_ms = min(
                self.active_sequence.noise_threshold_ms + 100, 5000
            )
        elif key == "-" and self.active_sequence:
            self.active_sequence.noise_threshold_ms = max(
                self.active_sequence.noise_threshold_ms - 100, 100
            )

    def _cursor_up(self):
        if self.mode == Mode.SCAN:
            self.cursor_pos = max(0, self.cursor_pos - 1)
        elif self.mode == Mode.MAP:
            self.map_cursor = max(0, self.map_cursor - 1)

    def _cursor_down(self):
        if self.mode == Mode.SCAN:
            self.cursor_pos = min(len(REGISTER_LIST) - 1, self.cursor_pos + 1)
        elif self.mode == Mode.MAP:
            unresolved = self.get_unresolved()
            self.map_cursor = min(max(0, len(unresolved) - 1), self.map_cursor + 1)

    def _start_tag_input(self):
        if self.mode == Mode.SCAN:
            key = self.selected_key
            if not key:
                return
        elif self.mode == Mode.MAP:
            unresolved = self.get_unresolved()
            if not unresolved or self.map_cursor >= len(unresolved):
                return
            key = unresolved[self.map_cursor][0]
            # Move scan cursor to this register for context
            if key in REGISTER_INDEX:
                self.cursor_pos = REGISTER_INDEX[key]
        else:
            return

        # Pre-fill with suggestion or existing tag
        tag = self.tag_store.get(*key)
        if tag:
            self.label_buffer = tag["name"]
        else:
            sug = self.get_suggestion(key)
            self.label_buffer = sug.name if sug else ""

        self.input_state = InputState.TAG_INPUT

    def _handle_tag_input_key(self, key):
        if key == "escape":
            self.input_state = InputState.NORMAL
            self.label_buffer = ""
            return
        if key == "enter":
            if self.label_buffer.strip():
                reg_key = self.selected_key
                if reg_key:
                    fc, addr = reg_key
                    # Get category from suggestion
                    sug = self.get_suggestion(reg_key)
                    category = sug.category if sug else ""
                    existing = self.tag_store.get(fc, addr)
                    confidence = existing["confidence"] if existing else 1
                    self.tag_store.set_tag(fc, addr, self.label_buffer.strip(), confidence, category)
            self.input_state = InputState.NORMAL
            self.label_buffer = ""
            return
        if key == "backspace":
            self.label_buffer = self.label_buffer[:-1]
            return
        if len(key) == 1 and key.isprintable():
            self.label_buffer += key

    def _set_confidence(self, level):
        if self.mode == Mode.SCAN:
            key = self.selected_key
        elif self.mode == Mode.MAP:
            unresolved = self.get_unresolved()
            if unresolved and self.map_cursor < len(unresolved):
                key = unresolved[self.map_cursor][0]
            else:
                return
        else:
            return

        if key and self.tag_store.is_tagged(*key):
            self.tag_store.set_confidence(*key, level)

    def _cycle_mode(self):
        if self.active_capture or self.active_sequence:
            self._finish_active()

        if self.mode == Mode.SCAN:
            self.pending_mode = Mode.CAPTURE
            self.input_state = InputState.LABEL_MENU
            self.mode = Mode.CAPTURE
        elif self.mode == Mode.CAPTURE:
            self.pending_mode = Mode.SEQUENCE
            self.input_state = InputState.LABEL_MENU
            self.mode = Mode.SEQUENCE
        elif self.mode == Mode.SEQUENCE:
            self.mode = Mode.MAP
            self.map_cursor = 0
        elif self.mode == Mode.MAP:
            self.mode = Mode.SCAN

    def _handle_label_menu_key(self, key):
        if key == "escape":
            self.input_state = InputState.NORMAL
            self.mode = Mode.SCAN
            self.pending_mode = None
            return
        if key == "c":
            self.input_state = InputState.LABEL_CUSTOM
            self.label_buffer = ""
            return
        if key.isdigit():
            idx = int(key) - 1
            if 0 <= idx < len(LABEL_PRESETS):
                self._start_recording(LABEL_PRESETS[idx])
                return

    def _handle_label_custom_key(self, key):
        if key == "escape":
            self.input_state = InputState.NORMAL
            self.mode = Mode.SCAN
            self.pending_mode = None
            self.label_buffer = ""
            return
        if key == "enter":
            if self.label_buffer.strip():
                self._start_recording(self.label_buffer.strip())
            return
        if key == "backspace":
            self.label_buffer = self.label_buffer[:-1]
            return
        if len(key) == 1 and key.isprintable():
            self.label_buffer += key

    def _start_recording(self, label):
        self.input_state = InputState.NORMAL
        now = time.monotonic()
        if self.mode == Mode.CAPTURE:
            snapshot = self.poller.snapshot() if self.poller else {}
            self.active_capture = CaptureSession(
                label=label,
                start_time=now,
                before_snapshot=snapshot,
            )
        elif self.mode == Mode.SEQUENCE:
            self.active_sequence = SequenceSession(
                label=label,
                start_time=now,
            )

    def _finish_active(self):
        if self.active_capture:
            self.active_capture.after_snapshot = self.poller.snapshot() if self.poller else {}
            self.captures.append(self.active_capture)
            self._save_capture(self.active_capture)
            self.active_capture = None
            self._save_report()
            self.mode = Mode.SCAN
        elif self.active_sequence:
            self.active_sequence.flush_all_bursts()
            self.sequences.append(self.active_sequence)
            self._save_sequence(self.active_sequence)
            self.active_sequence = None
            self._save_report()
            self.mode = Mode.SCAN

    def _cancel_active(self):
        self.active_capture = None
        self.active_sequence = None
        self.mode = Mode.SCAN
        self.input_state = InputState.NORMAL
        self.label_buffer = ""

    # ── Main loop ───────────────────────────────────────────────────────

    @property
    def connection_label(self):
        if self.serial_port:
            return f"{self.serial_port} @ {self.baudrate}bd"
        return f"{self.host}:{self.port}"

    async def run(self):
        if self.serial_port:
            self.client = AsyncModbusSerialClient(
                port=self.serial_port,
                baudrate=self.baudrate,
                parity="N",
                stopbits=1,
                bytesize=8,
            )
        else:
            self.client = AsyncModbusTcpClient(self.host, port=self.port)

        self.connected = await self.client.connect()
        if not self.connected:
            print(f"Failed to connect to {self.connection_label}")
            return

        self.poller = RegisterPoller(self.client, device_id=self.device_id)
        self._init_session_dir()

        # Load existing tags
        self.tag_store.load()

        fd = sys.stdin.fileno()
        self._old_termios = termios.tcgetattr(fd)

        last_activity_save = time.monotonic()
        last_poll = 0.0
        last_render = 0.0

        try:
            tty.setcbreak(fd)
            sys.stdout.write(HIDE_CURSOR)
            sys.stdout.flush()

            while self._running:
                try:
                    dirty = False
                    now = time.monotonic()

                    # Poll registers every 200ms
                    if now - last_poll >= 0.2:
                        changes = await self.poller.poll()
                        self.scan_count += 1
                        self._process_changes(changes)
                        last_poll = now
                        dirty = True

                    self._decay_highlights()

                    # Read one key per frame
                    key = self._read_key()
                    if key:
                        self._handle_key(key)
                        dirty = True

                    # Render on change, or every 250ms for decay/clock
                    now = time.monotonic()
                    if dirty or now - last_render >= 0.25:
                        screen = self.display.render(self)
                        sys.stdout.write(screen)
                        sys.stdout.flush()
                        last_render = now

                    if now - last_activity_save > 10.0:
                        self._save_activity()
                        last_activity_save = now

                    await asyncio.sleep(0.05)
                except (KeyboardInterrupt, asyncio.CancelledError):
                    break

        finally:
            sys.stdout.write(SHOW_CURSOR)
            sys.stdout.write(clear_screen())
            sys.stdout.flush()
            if self._old_termios:
                termios.tcsetattr(fd, termios.TCSADRAIN, self._old_termios)

            self._save_activity()
            self._save_report()
            self.tag_store.save()

            if self.csv_file:
                self.csv_file.close()
            if self.captures_file:
                self.captures_file.close()
            if self.sequences_file:
                self.sequences_file.close()
            if self.client:
                self.client.close()

            # Print summary
            n_tagged = sum(1 for k in REGISTER_LIST if self.tag_store.is_tagged(*k))
            score = self.readiness_score()
            print(f"\nSession saved to: {self.session_dir}/")
            print(f"  Scans: {self.scan_count}")
            print(f"  Changes detected: {self.total_changes}")
            print(f"  Captures: {len(self.captures)}")
            print(f"  Sequences: {len(self.sequences)}")
            print(f"  Tags: {n_tagged}/{len(REGISTER_LIST)}")
            print(f"  Readiness: {score}%")
            print(f"  Report: {os.path.join(self.session_dir, 'report.md')}")
            print(f"  Tags saved: {self.tag_store.path}")


def discover_connections(tcp_port=5020, baudrate=9600):
    connections = []

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        if sock.connect_ex(("127.0.0.1", tcp_port)) == 0:
            connections.append({
                "type": "tcp",
                "label": f"Simulator @ 127.0.0.1:{tcp_port}",
                "host": "127.0.0.1",
                "port": tcp_port,
            })
    finally:
        sock.close()

    for p in comports():
        if p.vid is not None:
            desc = p.description or "Unknown"
            connections.append({
                "type": "serial",
                "label": f"{p.device} - {desc} ({baudrate}bd)",
                "serial_port": p.device,
                "baudrate": baudrate,
            })

    return connections


def pick_connection(connections):
    if not connections:
        print("No connections found.")
        print("  - Start the simulator:  ./sim")
        print("  - Or specify manually:  ./scan --host <ip> or ./scan --serial <port>")
        return None

    print(f"\n{BOLD}Available connections:{RST}\n")
    for i, c in enumerate(connections, 1):
        print(f"  {CYAN}{i}){RST} {c['label']}")
    print()

    while True:
        try:
            choice = input(f"Select [1-{len(connections)}] or q to quit: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if choice.lower() == "q":
            return None
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(connections):
                return connections[idx]
        print(f"  Invalid choice. Enter 1-{len(connections)} or q.")


async def main(args):
    explicit = args.serial or args.host != "127.0.0.1"

    if explicit:
        app = ScannerApp(
            host=args.host,
            port=args.port,
            serial_port=args.serial,
            baudrate=args.baudrate,
            device_id=args.device_id,
        )
    else:
        connections = discover_connections(tcp_port=args.port, baudrate=args.baudrate)
        chosen = pick_connection(connections)
        if chosen is None:
            return

        if chosen["type"] == "tcp":
            app = ScannerApp(
                host=chosen["host"],
                port=chosen["port"],
                device_id=args.device_id,
            )
        else:
            app = ScannerApp(
                serial_port=chosen["serial_port"],
                baudrate=chosen["baudrate"],
                device_id=args.device_id,
            )

    await app.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Interactive PLC Register Scanner")
    parser.add_argument("--host", default="127.0.0.1", help="Modbus TCP host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5020, help="Modbus TCP port (default: 5020)")
    parser.add_argument("--serial", metavar="PORT", help="Serial port for RTU mode (e.g. /dev/tty.usbserial-1420)")
    parser.add_argument("--baudrate", type=int, default=9600, help="Serial baud rate (default: 9600)")
    parser.add_argument("--device-id", type=int, default=1, help="Modbus device/unit ID (default: 1)")
    args = parser.parse_args()

    asyncio.run(main(args))
