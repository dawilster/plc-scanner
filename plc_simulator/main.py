"""
PLC Simulator Entry Point

Runs:
  1. Modbus TCP server on port 5020 (simulating Delta DVP-14ES)
  2. Roll form machine state machine (100ms tick)
  3. Keyboard console for operator/sensor simulation

Usage:
  python -m plc_simulator.main [--port PORT]
"""

import asyncio
import argparse
import logging
import signal

from .server import build_datastore, run_server
from .machine import RollFormMachine
from .console import Console
from .registers import *

# Suppress noisy pymodbus exception logging (e.g. "Exception response 131 / 2")
logging.getLogger("pymodbus").setLevel(logging.CRITICAL)


def set_defaults(context):
    """Set initial register values matching the photo (622mm length setpoint)."""
    slave = context[0x00]

    # D registers - operating parameters
    slave.setValues(3, D_LENGTH_SETPOINT, [6112])   # D1 = 6112mm (from photo)
    slave.setValues(3, D_QTY_TARGET, [10])           # D3 = 10 pieces
    slave.setValues(3, D_FEED_SPEED, [50])           # D5 = 50 mm/sec
    slave.setValues(3, D_CUT_DWELL, [500])           # D9 = 500ms dwell
    slave.setValues(3, D_ENCODER_CAL, [100])         # D10 = 100 pulses/mm
    slave.setValues(3, D_MODE, [1])                   # D8 = Auto mode


def print_banner(port):
    print("=" * 70)
    print("  DELTA DVP-14ES PLC SIMULATOR")
    print("  Roll Form Cut-to-Length Machine")
    print("=" * 70)
    print(f"  Modbus TCP Server: 0.0.0.0:{port}")
    print(f"  Unit ID: 1")
    print()
    print("  Register Map (Delta DVP Convention):")
    print(f"    D registers : Holding Reg 0x1000 (4096) +offset")
    print(f"    M relays    : Coil 0x0800 (2048) +offset")
    print(f"    X inputs    : Discrete Input 0x0400 (1024) +offset")
    print(f"    Y outputs   : Coil 0x0500 (1280) +offset")
    print()
    print("  Defaults:")
    print(f"    Length setpoint: 6112mm (D1)")
    print(f"    Quantity target: 10 pcs (D3)")
    print(f"    Feed speed: 50 mm/sec (D5)")
    print(f"    Mode: Auto (D8=1)")
    print("=" * 70)


async def main(port=5020):
    context = build_datastore()
    set_defaults(context)

    machine = RollFormMachine(context)
    console = Console(context)

    print_banner(port)

    # Handle Ctrl+C gracefully
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def signal_handler():
        print("\n\nReceived shutdown signal...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    # Run all tasks concurrently
    server_task = asyncio.create_task(run_server(context, port=port))
    machine_task = asyncio.create_task(machine.run())
    console_task = asyncio.create_task(console.run())

    # Wait for console to exit (q key) or signal
    done, pending = await asyncio.wait(
        [server_task, machine_task, console_task, asyncio.create_task(stop_event.wait())],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Cancel remaining tasks
    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Delta DVP-14ES PLC Simulator")
    parser.add_argument("--port", type=int, default=5020, help="Modbus TCP port (default: 5020)")
    args = parser.parse_args()

    asyncio.run(main(port=args.port))
