"""
Black-Box Register Scanner

Connects to a Modbus TCP server (the simulator or a real PLC) and continuously
polls all Delta DVP address ranges. Highlights any register that changes value
and logs all changes with timestamps to a CSV file.

This is the tool you'd use on-site to reverse-engineer an unknown PLC register map.

Usage:
  python -m discovery.scanner [--host HOST] [--port PORT] [--csv LOGFILE]
"""

import asyncio
import argparse
import csv
import os
import sys
import time
from datetime import datetime

from pymodbus.client import AsyncModbusTcpClient


# Delta DVP address ranges to scan
SCAN_RANGES = {
    "hr": [  # Holding registers (D registers)
        {"name": "D0-D10", "start": 0x1000, "count": 11, "fc": 3},
        {"name": "D100-D102", "start": 0x1064, "count": 3, "fc": 3},
    ],
    "coil": [  # Coils (M relays + Y outputs)
        {"name": "M0-M21", "start": 0x0800, "count": 22, "fc": 1},
        {"name": "M100-M104", "start": 0x0864, "count": 5, "fc": 1},
        {"name": "Y0-Y5", "start": 0x0500, "count": 6, "fc": 1},
    ],
    "di": [  # Discrete inputs (X inputs)
        {"name": "X0-X7", "start": 0x0400, "count": 8, "fc": 2},
    ],
}

# Delta DVP symbol names for display
DELTA_SYMBOLS = {
    # D registers
    3: {0x1000 + i: f"D{i}" for i in range(11)},
    # D100-102
}
DELTA_SYMBOLS[3].update({0x1064 + i: f"D{100+i}" for i in range(3)})
# M relays
DELTA_SYMBOLS[1] = {0x0800 + i: f"M{i}" for i in range(22)}
DELTA_SYMBOLS[1].update({0x0864 + i: f"M{100+i}" for i in range(5)})
# Y outputs
DELTA_SYMBOLS[1].update({0x0500 + i: f"Y{i}" for i in range(6)})
# X inputs
DELTA_SYMBOLS[2] = {0x0400 + i: f"X{i}" for i in range(8)}


class RegisterScanner:
    def __init__(self, host="127.0.0.1", port=5020, csv_path=None):
        self.host = host
        self.port = port
        self.client = None
        self.previous_values = {}  # (fc, addr) -> value
        self.changed_addrs = set()  # recently changed addresses
        self.change_decay = {}  # (fc, addr) -> ticks remaining to highlight
        self.csv_path = csv_path or f"scan_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        self.csv_file = None
        self.csv_writer = None
        self.scan_count = 0

    async def connect(self):
        self.client = AsyncModbusTcpClient(self.host, port=self.port)
        connected = await self.client.connect()
        if not connected:
            print(f"Failed to connect to {self.host}:{self.port}")
            return False
        print(f"Connected to {self.host}:{self.port}")
        return True

    def _open_csv(self):
        self.csv_file = open(self.csv_path, "w", newline="")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(["timestamp", "scan_num", "fc", "address", "delta_symbol", "old_value", "new_value"])

    def _log_change(self, fc, addr, old_val, new_val):
        symbol = DELTA_SYMBOLS.get(fc, {}).get(addr, f"?{addr}")
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        if self.csv_writer:
            self.csv_writer.writerow([ts, self.scan_count, fc, addr, symbol, old_val, new_val])
            self.csv_file.flush()

    async def scan_once(self):
        """Poll all address ranges and detect changes."""
        self.scan_count += 1
        changes = []

        for category, ranges in SCAN_RANGES.items():
            for r in ranges:
                try:
                    if r["fc"] == 3:
                        result = await self.client.read_holding_registers(r["start"], count=r["count"], device_id=1)
                    elif r["fc"] == 1:
                        result = await self.client.read_coils(r["start"], count=r["count"], device_id=1)
                    elif r["fc"] == 2:
                        result = await self.client.read_discrete_inputs(r["start"], count=r["count"], device_id=1)
                    else:
                        continue

                    if result.isError():
                        continue

                    values = result.registers if r["fc"] == 3 else result.bits[:r["count"]]

                    for i, val in enumerate(values):
                        addr = r["start"] + i
                        key = (r["fc"], addr)
                        val_int = int(val)

                        if key in self.previous_values:
                            old = self.previous_values[key]
                            if val_int != old:
                                changes.append((r["fc"], addr, old, val_int))
                                self._log_change(r["fc"], addr, old, val_int)
                                self.change_decay[key] = 20  # Highlight for 20 ticks (2 sec)

                        self.previous_values[key] = val_int

                except Exception as e:
                    pass  # Silently skip read errors

        # Decay highlights
        for key in list(self.change_decay):
            self.change_decay[key] -= 1
            if self.change_decay[key] <= 0:
                del self.change_decay[key]

        return changes

    def display(self):
        """Print current register state to terminal."""
        lines = []
        lines.append("\033[2J\033[H")  # Clear screen, cursor to top
        lines.append("=" * 78)
        lines.append(f"  REGISTER SCANNER | {self.host}:{self.port} | Scan #{self.scan_count} | Log: {self.csv_path}")
        lines.append("=" * 78)

        # Holding registers (D)
        lines.append("\n  HOLDING REGISTERS (D registers) - FC3")
        lines.append("  " + "-" * 50)
        for r in SCAN_RANGES["hr"]:
            for i in range(r["count"]):
                addr = r["start"] + i
                key = (3, addr)
                val = self.previous_values.get(key, "?")
                symbol = DELTA_SYMBOLS.get(3, {}).get(addr, f"?{addr}")
                highlighted = key in self.change_decay
                if highlighted:
                    lines.append(f"  \033[1;33m  {symbol:>6} (addr {addr:5d}): {val:>6}  <<< CHANGED\033[0m")
                else:
                    lines.append(f"    {symbol:>6} (addr {addr:5d}): {val:>6}")

        # Coils - M relays
        lines.append("\n  COILS - M RELAYS - FC1")
        lines.append("  " + "-" * 50)
        for r in SCAN_RANGES["coil"]:
            if r["name"].startswith("M"):
                row = "  "
                for i in range(r["count"]):
                    addr = r["start"] + i
                    key = (1, addr)
                    val = self.previous_values.get(key, 0)
                    symbol = DELTA_SYMBOLS.get(1, {}).get(addr, "?")
                    highlighted = key in self.change_decay
                    if highlighted:
                        row += f"\033[1;33m{symbol}={'1' if val else '0'}\033[0m "
                    else:
                        row += f"{symbol}={'1' if val else '0'} "
                    if (i + 1) % 11 == 0:
                        lines.append(row)
                        row = "  "
                if row.strip():
                    lines.append(row)

        # Coils - Y outputs
        lines.append("\n  COILS - Y OUTPUTS - FC1")
        lines.append("  " + "-" * 50)
        row = "  "
        for r in SCAN_RANGES["coil"]:
            if r["name"].startswith("Y"):
                for i in range(r["count"]):
                    addr = r["start"] + i
                    key = (1, addr)
                    val = self.previous_values.get(key, 0)
                    symbol = DELTA_SYMBOLS.get(1, {}).get(addr, "?")
                    highlighted = key in self.change_decay
                    if highlighted:
                        row += f"\033[1;33m{symbol}={'1' if val else '0'}\033[0m "
                    else:
                        row += f"{symbol}={'1' if val else '0'} "
        lines.append(row)

        # Discrete inputs - X
        lines.append("\n  DISCRETE INPUTS - X INPUTS - FC2")
        lines.append("  " + "-" * 50)
        row = "  "
        for r in SCAN_RANGES["di"]:
            for i in range(r["count"]):
                addr = r["start"] + i
                key = (2, addr)
                val = self.previous_values.get(key, 0)
                symbol = DELTA_SYMBOLS.get(2, {}).get(addr, "?")
                highlighted = key in self.change_decay
                if highlighted:
                    row += f"\033[1;33m{symbol}={'1' if val else '0'}\033[0m "
                else:
                    row += f"{symbol}={'1' if val else '0'} "
        lines.append(row)

        lines.append("\n  " + "-" * 50)
        lines.append("  \033[1;33mYELLOW\033[0m = recently changed | Press Ctrl+C to stop")
        lines.append("")

        print("\n".join(lines), flush=True)


async def main(host, port, csv_path):
    scanner = RegisterScanner(host, port, csv_path)
    scanner._open_csv()

    if not await scanner.connect():
        return

    print("Scanning registers... (changes highlighted in yellow)")
    print("Press Ctrl+C to stop\n")

    try:
        while True:
            await scanner.scan_once()
            scanner.display()
            await asyncio.sleep(0.1)  # 100ms poll interval
    except KeyboardInterrupt:
        print("\n\nScanner stopped.")
        print(f"Change log saved to: {scanner.csv_path}")
    finally:
        if scanner.csv_file:
            scanner.csv_file.close()
        if scanner.client:
            scanner.client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Delta DVP Register Scanner")
    parser.add_argument("--host", default="127.0.0.1", help="Modbus TCP host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5020, help="Modbus TCP port (default: 5020)")
    parser.add_argument("--csv", default=None, help="CSV log file path")
    args = parser.parse_args()

    asyncio.run(main(args.host, args.port, args.csv))
