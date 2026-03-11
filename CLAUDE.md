# PLC Scanner Project

## What this is
Practice environment for reverse-engineering a **TouchWin TG765-MT HMI** + **Delta DVP-14ES PLC** controlling a metal roll form cut-to-length machine. Two main components:

1. **PLC Simulator** (`plc_simulator/`) - Python Modbus TCP server mimicking the DVP-14ES with realistic machine logic (state machine, hydraulics, encoder, cut cycle)
2. **Discovery Scanner** (`discovery/`) - Interactive register polling tool with 3 modes: SCAN (live view), CAPTURE (snapshot diffs), SEQUENCE (timestamped event log)

## Quick start

```bash
# Terminal 1 - Start PLC simulator (Modbus TCP on port 5020)
./sim
# or: .venv/bin/python -m plc_simulator.main [--port PORT]

# Terminal 2 - Start discovery scanner
./scan
# or: .venv/bin/python -m discovery.scanner [--host HOST] [--port PORT]
# Serial: .venv/bin/python -m discovery.scanner --serial /dev/ttyUSB0
```

## Project structure
```
plc_simulator/
  main.py          - Entry point, sets defaults, runs async event loop
  server.py        - Modbus TCP server (pymodbus ModbusSparseDataBlock)
  machine.py       - State machine: IDLE -> RUNNING -> CUTTING -> IDLE (+ FAULT/ESTOP)
  console.py       - Keyboard input for simulating operator/sensor actions
  registers.py     - All register address constants (D, M, X, Y base addresses)
discovery/
  scanner.py       - Interactive 3-mode scanner app (~900 lines)
  worksheet_template.md
docs/
  register_map.md  - Complete register map (answer key)
hmi_screens/       - SVG mockups of HMI screens
```

## Key technical details

- **pymodbus 3.12.x** - `ModbusSparseDataBlock` with `_OFFSET = 1` to compensate for `ModbusDeviceContext` internal address increment
- Delta DVP address mapping: D=HR@0x1000, M=Coil@0x0800, X=DI@0x0400, Y=Coil@0x0500
- Machine states: IDLE(0), RUNNING(1), CUTTING(2), FAULT(3), ESTOP(4)
- Console handles no-TTY gracefully (headless mode keeps server running)

## Dependencies
- Python 3.12+
- pymodbus >=3.8.0, <4.0.0
- pyserial (for serial connection mode in scanner)
- venv at `.venv/` - scripts `./sim` and `./scan` use `.venv/bin/python`

## Conventions
- Register constants use `D_`, `M_`, `X_`, `Y_` prefixes (see `registers.py`)
- HMI command bits are M11-M21 (set by console/HMI, cleared by machine after processing)
- Status bits are M0-M10 (set by machine, read by HMI/scanner)
- NC contacts (E-stop, pressure) default to 1 (healthy); 0 = tripped
