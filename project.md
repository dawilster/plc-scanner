# Roll Form Machine - PLC Simulator & HMI Practice Project

## What Is This?

A practice environment for reverse-engineering an obsolete **TouchWin TG765-MT** HMI + **Delta DVP-14ES** PLC controlling a metal roll form cut-to-length machine.

The project includes:
- **PLC Simulator** - Python Modbus TCP server mimicking a Delta DVP-14ES with realistic machine logic
- **Discovery Scanner** - Black-box register polling tool that logs changes (the tool you'd use on-site)
- **HMI Screen Mockups** - SVG images of the TouchWin screens with register address annotations
- **Register Map** - Complete I/O map (the "answer key" - don't peek until you've done discovery)

---

## Quick Start

### 1. Install dependencies

```bash
cd /path/to/plc-roll-form-project
pip install -r requirements.txt
```

### 2. Start the PLC simulator

```bash
python -m plc_simulator.main
```

This starts:
- Modbus TCP server on port 5020
- Machine state machine (100ms tick)
- Keyboard console for simulating operator actions and sensor events

### 3. Start the discovery scanner (in a second terminal)

```bash
python -m discovery.scanner
```

The scanner polls all Delta DVP address ranges and highlights any register changes in yellow.

### 4. Practice the workflow

In the simulator terminal, use keyboard keys to operate the machine:

**Operator keys** (simulates HMI button presses):
| Key | Action | PLC Address |
|-----|--------|-------------|
| `a` | Toggle Auto/Manual mode | D8 |
| `p` | Pump ON | M13 |
| `o` | Pump OFF | M14 |
| `s` | Auto Start | M11 |
| `x` | Stop / Reset | M12 |
| `f` | Jog FWD | M20 |
| `b` | Jog BACK | M21 |
| `c` | Manual Cut | M15 |
| `r` | Reset piece counter | D4 |

**Sensor keys** (simulates physical events):
| Key | Action | PLC Address |
|-----|--------|-------------|
| `e` | Toggle E-STOP | X7 |
| `m` | Toggle material present | X3 |
| `h` | Toggle hydraulic pressure OK | X4 |
| `t` | Toggle overtravel limit | X5 |

### 5. Typical auto cycle sequence

1. Press `a` to set Auto mode (if not already)
2. Press `p` to start the hydraulic pump (wait ~2s for pressure to build)
3. Press `s` to start the auto cycle
4. Watch the scanner: encoder counts up, then cut cycle, then counter increments
5. Cycle repeats until quantity target is reached
6. Press `x` to stop at any time

---

## Machine Overview

### What does a roll form cut-to-length machine do?

1. A coil of flat metal strip is loaded onto a decoiler
2. The strip feeds through a series of roll forming stations that progressively bend the flat strip into a profile shape
3. A rotary encoder measures the length of material fed through
4. When the programmed length is reached, the feed stops
5. A hydraulic shear/press cuts the piece
6. The cut piece drops into a collection bin
7. A counter tracks pieces produced vs the target quantity
8. The cycle repeats until the target count is reached

### This specific machine

- **PLC:** Delta DVP-14ES (8 digital inputs, 6 relay outputs, RS-232 + RS-485)
- **HMI:** TouchWin TG765-MT (4.3" touchscreen, connected via RS-232 or RS-485)
- **Product:** Blue metal profiles/strips, cut to 6112mm (~6m) lengths
- **Hydraulic:** Pump-driven hydraulic press for cutting

---

## The DVP-14ES PLC

### Communication Ports

| Port | Type | Connector | Typical Use |
|------|------|-----------|-------------|
| COM1 | RS-232 | Mini DIN 8-pin | Programming / laptop connection |
| COM2 | RS-485 | Terminal block | HMI connection (permanent) |

**Key insight:** Both ports operate independently. If the TG765 HMI is on COM2 (RS-485), you can plug your laptop into COM1 (RS-232) and monitor the PLC live while the machine runs. No disconnection needed.

### RS-232 Settings (Delta DVP default)

| Parameter | Value |
|-----------|-------|
| Baud rate | 9600 |
| Data bits | 7 |
| Parity | Even |
| Stop bits | 1 |
| Protocol | Delta DVP / Modbus RTU |

---

## Connecting EasyBuilder Pro

### To the simulator (practice)

1. Open EasyBuilder Pro, create new project (MT6050i or similar 480x272 model)
2. System Parameters > Device > New
3. Select driver: **MODBUS TCP/IP**
4. Settings:
   - IP: `127.0.0.1` (or host IP if EBPro runs in a VM)
   - Port: `5020`
   - Station: `1`
5. Use **Online Simulation** to connect live

### To the real DVP-14ES (on-site)

1. System Parameters > Device > New
2. Select driver: **Delta DVP** (under Delta category)
3. Settings:
   - COM port: your USB-to-RS232 adapter port
   - Baud: 9600, Data: 7, Parity: Even, Stop: 1
   - Station: `1`

### Address format in EBPro

EBPro adds 1 to all Modbus addresses (1-based convention):

| Register | Raw Address | Enter in EBPro |
|----------|------------|----------------|
| D0 | 4096 | 4097 |
| D1 | 4097 | 4098 |
| M0 | 2048 | 2049 |
| X0 | 1024 | 1025 |
| Y0 | 1280 | 1281 |

If using the native "Delta DVP" driver, you can use Delta-style addressing (D0, M0, etc.) and EBPro handles the translation automatically.

---

## Practice Workflow

### Phase 1: Black Box Discovery (do this first!)

**Goal:** Discover the register map without looking at `docs/register_map.md`.

1. Start the simulator and scanner in two terminals
2. Open `discovery/worksheet_template.md` - this is your blank register map
3. Operate the machine using keyboard keys
4. Watch the scanner for changes and fill in the worksheet
5. When done, compare against `docs/register_map.md`

### Phase 2: Build the HMI

1. Open the SVG mockups in `hmi_screens/` in a browser
2. Note the layout, buttons, displays, and register addresses
3. Recreate the screens in EasyBuilder Pro
4. Test with Online Simulation against the running Python server

### Phase 3: On-Site Rehearsal

1. Do Phase 1 again, blind, faster
2. Practice switching EBPro from Modbus TCP to Delta DVP RS-232 driver
3. Review the on-site checklist below

---

## On-Site Toolkit Checklist

### Hardware

- [ ] **Laptop** with EasyBuilder Pro installed (Windows required)
- [ ] **USB-to-RS232 adapter** (FTDI chipset recommended for reliability)
  - Get one with a mini DIN 8-pin cable if connecting to DVP COM1
  - Or USB-to-RS485 adapter if connecting to COM2
- [ ] **Ethernet cable** (in case there are other devices on the network)
- [ ] **Mini DIN 8-pin to DB9 cable** (Delta programming cable, sometimes called DVPCAB215)
- [ ] **Multimeter** for checking wiring
- [ ] **Camera/phone** for photographing HMI screens and wiring
- [ ] **Printed copy of `docs/register_map.md`** for annotation
- [ ] **Printed blank `discovery/worksheet_template.md`** for field notes

### Software

- [ ] **WPLSoft** (Delta's free PLC programming software)
  - Download from: https://www.deltaww.com (search "WPLSoft" in Downloads section)
  - Use this to upload the ladder program from the DVP-14ES
  - Online monitoring mode shows live register values in the ladder logic
- [ ] **EasyBuilder Pro** (Weintek's HMI development software)
  - Download from: https://www.weintek.com/globalw/Download/Download.aspx
  - You'll build the replacement HMI project in this
- [ ] **Modbus Poll** or **QModMaster** (free Modbus diagnostic tool)
  - Useful as a quick register scanner alongside or instead of the Python scanner
  - QModMaster: https://sourceforge.net/projects/qmodmaster/

### On-Site Procedure

1. **Photograph everything first** - every HMI screen, the wiring, the panel layout, model numbers
2. **Identify which port the TG765 is on** - check the cable from HMI to PLC
3. **Connect to the free port** - COM1 if HMI is on COM2, or vice versa
4. **Start with read-only** - don't write to any registers until you understand what they do
5. **Upload the ladder program** with WPLSoft - this is the most valuable artifact
6. **Run the scanner** while the operator runs the machine normally
7. **Fill in your worksheet** based on observations
8. **Build the EBPro project** on-site or back at the office
9. **Test the new HMI** by connecting EBPro to the PLC and verifying register mappings

### Important Notes

- The DVP-14ES default RS-232 settings are 9600/7/Even/1 - if these don't work, check the PLC's communication parameters in WPLSoft
- The PLC station/unit address is typically 1, but check in WPLSoft
- **Never write to registers on a running machine** unless you understand what they control - you could trigger unexpected machine movement
- The E-stop should always be within reach during testing
- On-site, change your simulator settings: IP becomes the real PLC's IP, port changes from 5020 to 502 (or COM port for RS-232)

---

## File Reference

| File | Description |
|------|-------------|
| `plc_simulator/main.py` | Entry point - start with `python -m plc_simulator.main` |
| `plc_simulator/registers.py` | All register address constants |
| `plc_simulator/server.py` | Modbus TCP server setup |
| `plc_simulator/machine.py` | State machine and physics simulation |
| `plc_simulator/console.py` | Keyboard console for operator simulation |
| `discovery/scanner.py` | Register scanner - `python -m discovery.scanner` |
| `discovery/worksheet_template.md` | Blank register map worksheet |
| `docs/register_map.md` | Complete register map (answer key) |
| `hmi_screens/01_operate_main.svg` | Main OPERATE screen mockup |
| `hmi_screens/02_sys_set.svg` | System settings screen mockup |
| `hmi_screens/03_alarm_fault.svg` | Alarm/fault screen mockup |

---

## Troubleshooting

**"Connection refused" when running scanner:**
- Make sure the simulator is running first (`python -m plc_simulator.main`)
- Check the port matches (default 5020)

**EasyBuilder Pro can't connect:**
- If EBPro is in a Windows VM, use the host machine's IP (not 127.0.0.1)
- Check macOS firewall isn't blocking port 5020
- Verify the EBPro device is set to MODBUS TCP/IP (not Delta DVP)

**Port 5020 already in use:**
- Use `--port 5021` or any other free port
- Kill any previous simulator instance: `lsof -ti:5020 | xargs kill`

**Registers read as 0 / no changes detected:**
- Verify `zero_mode=True` in server.py (off-by-one address issue)
- Check the scanner is polling the right address ranges
- Try reading address 4096 (D0) with a Modbus client to verify connectivity
