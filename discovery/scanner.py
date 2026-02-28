"""
Interactive PLC Register Scanner - Reverse Engineering Workstation

Three-mode interactive tool for discovering unknown PLC register maps:
  SCAN     - Live register view with change highlighting
  CAPTURE  - Snapshot before/after a single action (e.g. press a button)
  SEQUENCE - Record a full machine cycle as a timestamped event log

Usage:
  python -m discovery.scanner [--host HOST] [--port PORT]
  python -m discovery.scanner --serial /dev/tty.usbserial-1420 [--baudrate 9600] [--device-id 1]
"""

import asyncio
import argparse
import csv
import json
import os
import socket
import sys
import select
import termios
import tty
import time
from datetime import datetime
from enum import Enum, auto
from dataclasses import dataclass, field

from serial.tools.list_ports import comports
from pymodbus.client import AsyncModbusTcpClient, AsyncModbusSerialClient


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


# ── ANSI helpers ────────────────────────────────────────────────────────────

RST = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
YELLOW = "\033[1;33m"
GREEN = "\033[32m"
RED = "\033[31m"
CYAN = "\033[36m"
WHITE = "\033[37m"
BG_RED = "\033[41m"
INVERSE = "\033[7m"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"


def goto(row, col):
    return f"\033[{row};{col}H"


def clear_screen():
    return "\033[2J\033[H"


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


# ── Data classes ────────────────────────────────────────────────────────────

class Mode(Enum):
    SCAN = auto()
    CAPTURE = auto()
    SEQUENCE = auto()


class InputState(Enum):
    NORMAL = auto()
    LABEL_MENU = auto()
    LABEL_CUSTOM = auto()


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
    before_snapshot: dict = field(default_factory=dict)  # (fc,addr)->val stored as str keys
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
    collapsed_events: list = field(default_factory=list)  # For display
    burst_trackers: dict = field(default_factory=dict)  # (fc,addr) -> {first_val, last_val, count, first_t, last_t}
    noise_threshold_ms: int = 500

    def duration(self):
        if self.events:
            return self.events[-1].timestamp - self.start_time
        return time.monotonic() - self.start_time

    def add_event(self, event):
        """Add event with burst collapsing for noisy registers."""
        self.events.append(event)
        key = (event.fc, event.addr)

        # Bit registers always log individually
        if is_bit_register(event.fc, event.addr):
            self.collapsed_events.append(event)
            return

        threshold_s = self.noise_threshold_ms / 1000.0
        if key in self.burst_trackers:
            tracker = self.burst_trackers[key]
            elapsed = event.timestamp - tracker["last_t"]
            if elapsed < threshold_s:
                # Within burst - update tracker
                tracker["last_val"] = event.new_val
                tracker["count"] += 1
                tracker["last_t"] = event.timestamp
                return
            else:
                # Burst ended - flush it
                self._flush_burst(key)

        # Start new tracker
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
            # Replace last collapsed event for this key with a summary
            for i in range(len(self.collapsed_events) - 1, -1, -1):
                e = self.collapsed_events[i]
                if (e.fc, e.addr) == key:
                    # Mark as collapsed burst by adding a synthetic event
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
        self.values = {}  # (fc, addr) -> int

    async def poll(self):
        """Poll all ranges, return list of ChangeEvent for anything that changed."""
        changes = []
        now = time.monotonic()

        for _cat, ranges in SCAN_RANGES.items():
            for r in ranges:
                try:
                    if r["fc"] == 3:
                        result = await self.client.read_holding_registers(
                            r["start"], count=r["count"], device_id=self.device_id
                        )
                    elif r["fc"] == 1:
                        result = await self.client.read_coils(
                            r["start"], count=r["count"], device_id=self.device_id
                        )
                    elif r["fc"] == 2:
                        result = await self.client.read_discrete_inputs(
                            r["start"], count=r["count"], device_id=self.device_id
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
    def generate(session_meta, captures, sequences, activity):
        lines = []
        ts = datetime.fromtimestamp(session_meta["start_wall"]).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"# PLC Discovery Report - {ts}")
        lines.append("")
        lines.append(f"## Connection: {session_meta['connection']}")
        dur = session_meta.get("duration_s", 0)
        lines.append(f"## Session duration: {dur:.1f}s | Total scans: {session_meta.get('scan_count', 0)}")
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
                            # Find first old and last new
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
                for ev in seq["events"][:200]:  # Cap at 200 for readability
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
        # Strip ANSI for length calculation
        clean = self._strip_ansi(text)
        pad = self.width - 2 - len(clean)
        if pad < 0:
            pad = 0
        return side + text + " " * pad + side

    @staticmethod
    def _strip_ansi(text):
        import re
        return re.sub(r'\033\[[^m]*m', '', text)

    def render(self, app):
        """Build full screen content as a single string.

        Uses cursor-home + overwrite instead of clear-screen to avoid flicker.
        Each line is padded to full width so old content is overwritten.
        """
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

        # Register area - calculate available lines
        bottom_panel_lines = 6
        if app.mode == Mode.CAPTURE and app.active_capture:
            bottom_panel_lines = max(6, 4 + min(len(app.active_capture.changes), 8))
        elif app.mode == Mode.SEQUENCE and app.active_sequence:
            bottom_panel_lines = max(6, 4 + min(len(app.active_sequence.collapsed_events), 10))
        elif app.input_state != InputState.NORMAL:
            bottom_panel_lines = 5

        reg_lines = self.height - 6 - bottom_panel_lines  # 6 = header + borders
        reg_lines = max(reg_lines, 10)

        # Build register content
        reg_content = self._render_registers(app)
        for i, line in enumerate(reg_content[:reg_lines]):
            lines.append(self._padline(line))
        # Pad remaining lines
        for _ in range(reg_lines - min(len(reg_content), reg_lines)):
            lines.append(self._padline(""))

        # Bottom separator
        lines.append(self._hline("╠", "═", "╣"))

        # Bottom panel
        bottom = self._render_bottom(app)
        for line in bottom:
            lines.append(self._padline(line))

        # Bottom border
        lines.append(self._hline("╚", "═", "╝"))

        # Blank any leftover terminal rows below our frame
        total_rows = len(lines)
        for _ in range(self.height - total_rows):
            lines.append("\033[K")  # erase-to-EOL for any trailing rows

        # Cursor home + hide, then single write of the full frame
        return HIDE_CURSOR + "\033[H" + "\n".join(lines)

    def _render_registers(self, app):
        """Render the register values for the main panel."""
        lines = []
        poller = app.poller
        if not poller or not poller.values:
            lines.append("  Waiting for first scan...")
            return lines

        # Two-column layout: D registers on left, coils/IO on right
        left_lines = []
        right_lines = []

        # LEFT COLUMN: D registers
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
                if key in app.change_decay:
                    left_lines.append(f"  {ever} {YELLOW}{sym:>4} [{addr}]: {val:>6}{RST}{recent}")
                elif key in app.ever_changed:
                    left_lines.append(f"  {CYAN}{ever}{RST} {sym:>4} [{addr}]: {val:>6}{recent}")
                else:
                    left_lines.append(f"  {DIM}{ever} {sym:>4} [{addr}]: {val:>6}{RST}{recent}")

        # RIGHT COLUMN: M relays
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
                if key in app.change_decay:
                    row_items.append(f"{YELLOW}{sym}={'1' if val else '0'}{RST}")
                elif val:
                    row_items.append(f"{GREEN}{sym}={val}{RST}")
                else:
                    row_items.append(f"{DIM}{sym}={val}{RST}")
                if len(row_items) == 4:
                    right_lines.append("  " + "  ".join(row_items))
                    row_items = []
            if row_items:
                right_lines.append("  " + "  ".join(row_items))

        # Y outputs
        right_lines.append("")
        right_lines.append(f"  {BOLD}Y OUTPUTS        X INPUTS{RST}")
        right_lines.append(f"  {'─' * 12}       {'─' * 12}")
        y_range = [r for r in SCAN_RANGES["coil"] if r["name"].startswith("Y")][0]
        x_range = SCAN_RANGES["di"][0]
        max_io = max(y_range["count"], x_range["count"])
        for row_i in range(0, max_io, 2):
            parts = "  "
            # Two Y per line
            for j in range(2):
                idx = row_i + j
                if idx < y_range["count"]:
                    addr = y_range["start"] + idx
                    key = (1, addr)
                    val = poller.values.get(key, 0)
                    sym = symbol(1, addr)
                    if key in app.change_decay:
                        parts += f"{YELLOW}{sym}={'1' if val else '0'}{RST} "
                    elif val:
                        parts += f"{GREEN}{sym}={'1' if val else '0'}{RST} "
                    else:
                        parts += f"{DIM}{sym}={'1' if val else '0'}{RST} "
                else:
                    parts += "      "
            parts += "       "
            # Two X per line
            for j in range(2):
                idx = row_i + j
                if idx < x_range["count"]:
                    addr = x_range["start"] + idx
                    key = (2, addr)
                    val = poller.values.get(key, 0)
                    sym = symbol(2, addr)
                    if key in app.change_decay:
                        parts += f"{YELLOW}{sym}={'1' if val else '0'}{RST} "
                    elif val:
                        parts += f"{GREEN}{sym}={'1' if val else '0'}{RST} "
                    else:
                        parts += f"{DIM}{sym}={'1' if val else '0'}{RST} "
            right_lines.append(parts)

        # Merge left and right columns
        col_width = (self.width - 4) // 2
        max_lines = max(len(left_lines), len(right_lines))
        for i in range(max_lines):
            left = left_lines[i] if i < len(left_lines) else ""
            right = right_lines[i] if i < len(right_lines) else ""
            # Strip ANSI for padding
            import re
            left_clean = re.sub(r'\033\[[^m]*m', '', left)
            pad = col_width - len(left_clean)
            if pad < 0:
                pad = 0
            lines.append(left + " " * pad + right)

        return lines

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

        if app.mode == Mode.CAPTURE and app.active_capture:
            cap = app.active_capture
            n_changes = len(cap.changes)
            lines.append(f"  {RED}◉{RST} CAPTURE: \"{cap.label}\"" +
                         f"                    {DIM}[Enter=finish  Esc=cancel]{RST}")
            lines.append(f"  Recording... {n_changes} change{'s' if n_changes != 1 else ''} detected")
            # Show recent changes (last 6)
            recent = cap.changes[-6:]
            for ch in recent:
                wall = datetime.fromtimestamp(
                    app.start_wall + (ch.timestamp - app.start_mono)
                ).strftime("%H:%M:%S.%f")[:-3]
                lines.append(f"    {wall}  {ch.symbol:>4}  [{ch.addr}]: {ch.old_val} -> {ch.new_val}")
            # Pad to minimum height
            while len(lines) < 4:
                lines.append("")
            return lines

        if app.mode == Mode.SEQUENCE and app.active_sequence:
            seq = app.active_sequence
            dur = seq.duration()
            n_events = len(seq.events)
            lines.append(
                f"  {RED}◉{RST} SEQUENCE: \"{seq.label}\""
                f"          duration: {dur:.1f}s  "
                f"{DIM}[Enter=stop  Esc=cancel  +/-=threshold:{seq.noise_threshold_ms}ms]{RST}"
            )
            lines.append(f"  Timeline ({n_events} events):")
            # Show recent collapsed events (last 8)
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

        # Default: SCAN mode footer
        n_captures = len(app.captures)
        n_total = app.total_changes
        lines.append(
            f"  {CYAN}●{RST} = changed this session  "
            f"{YELLOW}◄{RST} = changed in last 2s"
        )
        lines.append(
            f"  Captures: {n_captures} saved | "
            f"Sequences: {len(app.sequences)} saved | "
            f"Changes: {n_total} total | "
            f"Session: {app.session_dir_short}"
        )
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
        self.ever_changed = set()     # (fc, addr) that have ever changed
        self.change_decay = {}        # (fc, addr) -> monotonic time when highlight expires

        # Sessions
        self.active_capture = None
        self.active_sequence = None
        self.captures = []
        self.sequences = []

        # Activity tracker: (fc,addr) -> {count, min, max, last, values_set}
        self.activity = {}

        # Input
        self.label_buffer = ""
        self.pending_mode = None  # Mode we're entering after label input

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

    def _init_session_dir(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_dir = os.path.join("discovery", "sessions", ts)
        self.session_dir_short = f"sessions/{ts}"
        os.makedirs(self.session_dir, exist_ok=True)

        # CSV log
        csv_path = os.path.join(self.session_dir, "scan_log.csv")
        self.csv_file = open(csv_path, "w", newline="")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(["timestamp", "scan_num", "fc", "address", "symbol", "old_value", "new_value"])

        # JSONL files (append mode)
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

        # Build activity dict for report
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

        report_md = ReportGenerator.generate(meta, cap_dicts, seq_dicts, act)
        path = os.path.join(self.session_dir, "report.md")
        with open(path, "w") as f:
            f.write(report_md)

    def _track_change(self, event):
        """Update tracking structures for a change event."""
        key = (event.fc, event.addr)
        self.ever_changed.add(key)
        self.change_decay[key] = time.monotonic() + 2.0  # 2 second highlight
        self.total_changes += 1

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

    def _process_changes(self, changes):
        """Route change events to the appropriate handlers."""
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
        """Non-blocking read of a keypress. Returns key string or None."""
        if not select.select([sys.stdin], [], [], 0)[0]:
            return None
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            # Could be escape or arrow key sequence
            if select.select([sys.stdin], [], [], 0.05)[0]:
                # Arrow key sequence - consume and ignore
                sys.stdin.read(1)
                if select.select([sys.stdin], [], [], 0.01)[0]:
                    sys.stdin.read(1)
                return None
            return "escape"
        if ch == "\t":
            return "tab"
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x7f":
            return "backspace"
        return ch

    def _handle_key(self, key):
        """Process a keypress based on current state."""
        if self.input_state == InputState.LABEL_MENU:
            self._handle_label_menu_key(key)
            return
        if self.input_state == InputState.LABEL_CUSTOM:
            self._handle_label_custom_key(key)
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
        elif key == "+" and self.active_sequence:
            self.active_sequence.noise_threshold_ms = min(
                self.active_sequence.noise_threshold_ms + 100, 5000
            )
        elif key == "-" and self.active_sequence:
            self.active_sequence.noise_threshold_ms = max(
                self.active_sequence.noise_threshold_ms - 100, 100
            )

    def _cycle_mode(self):
        """Tab cycles: SCAN -> CAPTURE -> SEQUENCE -> SCAN."""
        if self.active_capture or self.active_sequence:
            # Finish current recording first
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
        # Number keys 1-8 for presets
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
        # Connect
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

        # Set terminal to cbreak mode
        fd = sys.stdin.fileno()
        self._old_termios = termios.tcgetattr(fd)

        last_activity_save = time.monotonic()

        try:
            tty.setcbreak(fd)
            sys.stdout.write(HIDE_CURSOR)
            sys.stdout.flush()

            while self._running:
                try:
                    # Poll registers
                    changes = await self.poller.poll()
                    self.scan_count += 1
                    self._process_changes(changes)
                    self._decay_highlights()

                    # Read keyboard (non-blocking)
                    key = self._read_key()
                    if key:
                        self._handle_key(key)

                    # Render
                    screen = self.display.render(self)
                    sys.stdout.write(screen)
                    sys.stdout.flush()

                    # Periodic activity save (~10s)
                    now = time.monotonic()
                    if now - last_activity_save > 10.0:
                        self._save_activity()
                        last_activity_save = now

                    await asyncio.sleep(0.1)
                except (KeyboardInterrupt, asyncio.CancelledError):
                    break

        finally:
            # Restore terminal
            sys.stdout.write(SHOW_CURSOR)
            sys.stdout.write(clear_screen())
            sys.stdout.flush()
            if self._old_termios:
                termios.tcsetattr(fd, termios.TCSADRAIN, self._old_termios)

            # Final saves
            self._save_activity()
            self._save_report()

            # Close files
            if self.csv_file:
                self.csv_file.close()
            if self.captures_file:
                self.captures_file.close()
            if self.sequences_file:
                self.sequences_file.close()
            if self.client:
                self.client.close()

            # Print summary
            print(f"\nSession saved to: {self.session_dir}/")
            print(f"  Scans: {self.scan_count}")
            print(f"  Changes detected: {self.total_changes}")
            print(f"  Captures: {len(self.captures)}")
            print(f"  Sequences: {len(self.sequences)}")
            print(f"  Report: {os.path.join(self.session_dir, 'report.md')}")


def discover_connections(tcp_port=5020, baudrate=9600):
    """Find available Modbus connections: simulator on TCP + USB serial adapters."""
    connections = []

    # Probe simulator on localhost
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

    # Enumerate USB serial ports
    for p in comports():
        if p.vid is not None:  # USB device
            desc = p.description or "Unknown"
            connections.append({
                "type": "serial",
                "label": f"{p.device} - {desc} ({baudrate}bd)",
                "serial_port": p.device,
                "baudrate": baudrate,
            })

    return connections


def pick_connection(connections):
    """Present connection choices to the user. Returns chosen dict or None."""
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
    # If user gave explicit connection args, connect directly
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
        # Auto-discover
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
