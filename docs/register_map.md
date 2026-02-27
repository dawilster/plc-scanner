# Delta DVP-14ES Register Map - Roll Form Cut-to-Length Machine

## ANSWER KEY - Don't look at this until you've completed the discovery worksheet!

---

## Delta DVP Address Convention

Delta DVP PLCs use offset-based Modbus addressing:

| Device Type | DVP Symbol | Modbus Base Address | Modbus Function Codes |
|-------------|-----------|--------------------|-----------------------|
| Data Registers | D | 0x1000 (4096) | FC3 Read / FC6,FC16 Write |
| Internal Relays | M | 0x0800 (2048) | FC1 Read / FC5,FC15 Write |
| Digital Inputs | X | 0x0400 (1024) | FC2 Read |
| Digital Outputs | Y | 0x0500 (1280) | FC1 Read / FC5,FC15 Write |

**EasyBuilder Pro note:** EBPro uses 1-based addressing in its UI. Add 1 to all addresses below when entering them in EBPro (e.g., D0 at raw 4096 = enter "4097" in EBPro).

---

## D Registers (Holding Registers)

| Symbol | Raw Addr | EBPro Addr | Hex | Description | Units | Range |
|--------|----------|-----------|------|-------------|-------|-------|
| D0 | 4096 | 4097 | 0x1000 | Machine state | enum | 0-4 |
| D1 | 4097 | 4098 | 0x1001 | Length setpoint | mm | 0-9999 |
| D2 | 4098 | 4099 | 0x1002 | Current feed length (encoder) | mm | 0-9999 |
| D3 | 4099 | 4100 | 0x1003 | Quantity target | count | 0-9999 |
| D4 | 4100 | 4101 | 0x1004 | Quantity current (pieces cut) | count | 0-9999 |
| D5 | 4101 | 4102 | 0x1005 | Feed speed setpoint | mm/sec | 0-500 |
| D6 | 4102 | 4103 | 0x1006 | Hydraulic pressure | bar | 0-200 |
| D7 | 4103 | 4104 | 0x1007 | Active fault code | enum | 0-4 |
| D8 | 4104 | 4105 | 0x1008 | Mode (0=Manual, 1=Auto) | enum | 0-1 |
| D9 | 4105 | 4106 | 0x1009 | Cut dwell time | ms | 0-5000 |
| D10 | 4106 | 4107 | 0x100A | Encoder calibration | pulses/mm | 1-1000 |
| D100 | 4196 | 4197 | 0x1064 | Fault history slot 0 (newest) | code | 0-4 |
| D101 | 4197 | 4198 | 0x1065 | Fault history slot 1 | code | 0-4 |
| D102 | 4198 | 4199 | 0x1066 | Fault history slot 2 (oldest) | code | 0-4 |

### Machine State Values (D0)

| Value | State | Description |
|-------|-------|-------------|
| 0 | IDLE | Machine stopped, ready for commands |
| 1 | RUNNING | Feed motor active, encoder counting |
| 2 | CUTTING | Hydraulic press cycle in progress |
| 3 | FAULT | Fault condition active, outputs off |
| 4 | ESTOP | Emergency stop, all outputs off |

### Fault Code Values (D7)

| Value | Fault | Description |
|-------|-------|-------------|
| 0 | None | No active fault |
| 1 | E-Stop | Emergency stop button pressed |
| 2 | Low Pressure | Hydraulic pressure below 30 bar |
| 3 | Encoder | Encoder signal fault |
| 4 | Overtravel | Material overtravel limit hit |

---

## M Relays (Coils) - Status Flags

| Symbol | Raw Addr | EBPro Addr | Hex | Description | R/W |
|--------|----------|-----------|------|-------------|-----|
| M0 | 2048 | 2049 | 0x0800 | Auto mode active | R |
| M1 | 2049 | 2050 | 0x0801 | Pump running | R |
| M2 | 2050 | 2051 | 0x0802 | Currently cutting | R |
| M3 | 2051 | 2052 | 0x0803 | Feed forward active | R |
| M4 | 2052 | 2053 | 0x0804 | Feed reverse active | R |
| M5 | 2053 | 2054 | 0x0805 | E-stop latched | R |
| M6 | 2054 | 2055 | 0x0806 | Fault present | R |
| M7 | 2055 | 2056 | 0x0807 | Quantity target reached | R |
| M8 | 2056 | 2057 | 0x0808 | Cut solenoid energised | R |
| M9 | 2057 | 2058 | 0x0809 | Length reached (one-shot) | R |
| M10 | 2058 | 2059 | 0x080A | Auto cycle running | R |

## M Relays (Coils) - HMI Command Bits

These are written by the HMI (or your scanner) to command the PLC:

| Symbol | Raw Addr | EBPro Addr | Hex | Description | R/W |
|--------|----------|-----------|------|-------------|-----|
| M11 | 2059 | 2060 | 0x080B | Cmd: Auto Start | W |
| M12 | 2060 | 2061 | 0x080C | Cmd: Stop / Reset | W |
| M13 | 2061 | 2062 | 0x080D | Cmd: Pump ON | W |
| M14 | 2062 | 2063 | 0x080E | Cmd: Pump OFF | W |
| M15 | 2063 | 2064 | 0x080F | Cmd: Manual Cut | W |
| M20 | 2068 | 2069 | 0x0814 | Cmd: Jog FWD | W |
| M21 | 2069 | 2070 | 0x0815 | Cmd: Jog BACK | W |

## M Relays (Coils) - Alarm Bits

| Symbol | Raw Addr | EBPro Addr | Hex | Description |
|--------|----------|-----------|------|-------------|
| M100 | 2148 | 2149 | 0x0864 | Alarm: E-stop active |
| M101 | 2149 | 2150 | 0x0865 | Alarm: Hydraulic pressure low |
| M102 | 2150 | 2151 | 0x0866 | Alarm: Encoder fault |
| M103 | 2151 | 2152 | 0x0867 | Alarm: Overtravel limit |
| M104 | 2152 | 2153 | 0x0868 | Alarm: Quantity target reached |

---

## X Inputs (Discrete Inputs) - DVP-14ES: 8 inputs (X0-X7)

| Symbol | Raw Addr | Hex | Description | Wiring |
|--------|----------|------|-------------|--------|
| X0 | 1024 | 0x0400 | Encoder channel A | Rotary encoder |
| X1 | 1025 | 0x0401 | Encoder channel B | Rotary encoder |
| X2 | 1026 | 0x0402 | Cut home/retract limit | Limit switch |
| X3 | 1027 | 0x0403 | Material present sensor | Proximity sensor |
| X4 | 1028 | 0x0404 | Hydraulic pressure OK (NC) | Pressure switch |
| X5 | 1029 | 0x0405 | Overtravel limit switch | Limit switch |
| X6 | 1030 | 0x0406 | Guard/door interlock | Safety switch |
| X7 | 1031 | 0x0407 | E-STOP (NC: 1=normal, 0=tripped) | E-stop mushroom |

**Note:** Physical panel buttons (FWD, BACK, PUMP ON, PUMP OFF, CUT, START) are wired to the TG765-MT HMI, NOT to PLC X inputs. The HMI reads button presses and writes to M relay command bits (M11-M21). The DVP-14ES only has 8 inputs, which are all used for sensors.

---

## Y Outputs (Coils) - DVP-14ES: 6 relay outputs (Y0-Y5)

| Symbol | Raw Addr | Hex | Description | Actuator |
|--------|----------|------|-------------|----------|
| Y0 | 1280 | 0x0500 | Feed motor FWD contactor | AC motor/VFD |
| Y1 | 1281 | 0x0501 | Feed motor REV contactor | AC motor/VFD |
| Y2 | 1282 | 0x0502 | Cut solenoid (extend) | Hydraulic solenoid |
| Y3 | 1283 | 0x0503 | Cut retract solenoid | Hydraulic solenoid |
| Y4 | 1284 | 0x0504 | Hydraulic pump motor | Motor contactor |
| Y5 | 1285 | 0x0505 | Run/fault indicator lamp | Pilot lamp |
