"""
Delta DVP-14ES Register Address Constants

All addresses are raw Modbus protocol addresses (0-indexed, as sent on the wire).
Delta DVP convention:
  D registers -> Holding Registers (FC3/FC6/FC16) starting at 0x1000
  M relays    -> Coils (FC1/FC5/FC15) starting at 0x0800
  X inputs    -> Discrete Inputs (FC2) starting at 0x0400
  Y outputs   -> Coils (FC1/FC5/FC15) starting at 0x0500

Note: EasyBuilder Pro uses 1-based addressing in its UI (adds 1 to these values).
"""

# === Delta DVP Base Offsets ===
D_BASE = 0x1000  # 4096 - D0 starts here in holding registers
M_BASE = 0x0800  # 2048 - M0 starts here in coils
X_BASE = 0x0400  # 1024 - X0 starts here in discrete inputs
Y_BASE = 0x0500  # 1280 - Y0 starts here in coils


# === D Registers (Holding Registers) ===
D_MACHINE_STATE   = D_BASE + 0   # 4096 - D0: 0=IDLE,1=RUNNING,2=CUTTING,3=FAULT,4=ESTOP
D_LENGTH_SETPOINT = D_BASE + 1   # 4097 - D1: Length setpoint (mm)
D_CURRENT_LENGTH  = D_BASE + 2   # 4098 - D2: Current feed length / encoder (mm)
D_QTY_TARGET      = D_BASE + 3   # 4099 - D3: Quantity target
D_QTY_CURRENT     = D_BASE + 4   # 4100 - D4: Quantity current (pieces cut)
D_FEED_SPEED      = D_BASE + 5   # 4101 - D5: Feed speed setpoint (mm/sec)
D_HYD_PRESSURE    = D_BASE + 6   # 4102 - D6: Hydraulic pressure (bar)
D_FAULT_CODE      = D_BASE + 7   # 4103 - D7: Active fault code
D_MODE            = D_BASE + 8   # 4104 - D8: Mode (0=Manual, 1=Auto)
D_CUT_DWELL       = D_BASE + 9   # 4105 - D9: Cut dwell time (ms)
D_ENCODER_CAL     = D_BASE + 10  # 4106 - D10: Encoder pulses/mm calibration

# Fault history
D_FAULT_HIST_0    = D_BASE + 100  # 4196 - D100
D_FAULT_HIST_1    = D_BASE + 101  # 4197 - D101
D_FAULT_HIST_2    = D_BASE + 102  # 4198 - D102


# === Machine State Enum ===
STATE_IDLE    = 0
STATE_RUNNING = 1
STATE_CUTTING = 2
STATE_FAULT   = 3
STATE_ESTOP   = 4

STATE_NAMES = {
    STATE_IDLE: "IDLE",
    STATE_RUNNING: "RUNNING",
    STATE_CUTTING: "CUTTING",
    STATE_FAULT: "FAULT",
    STATE_ESTOP: "ESTOP",
}


# === Fault Code Enum ===
FAULT_NONE       = 0
FAULT_ESTOP      = 1
FAULT_LOW_PRES   = 2
FAULT_ENCODER    = 3
FAULT_OVERTRAVEL = 4

FAULT_NAMES = {
    FAULT_NONE: "No Fault",
    FAULT_ESTOP: "E-Stop Active",
    FAULT_LOW_PRES: "Hydraulic Pressure Low",
    FAULT_ENCODER: "Encoder Fault",
    FAULT_OVERTRAVEL: "Overtravel Limit",
}


# === M Relays (Coils - status/flags) ===
M_AUTO_ACTIVE     = M_BASE + 0   # 2048 - M0: Auto mode active flag
M_PUMP_RUNNING    = M_BASE + 1   # 2049 - M1: Pump running flag
M_CUTTING         = M_BASE + 2   # 2050 - M2: Currently cutting flag
M_FEED_FWD        = M_BASE + 3   # 2051 - M3: Feed forward active flag
M_FEED_REV        = M_BASE + 4   # 2052 - M4: Feed reverse active flag
M_ESTOP_LATCHED   = M_BASE + 5   # 2053 - M5: E-stop latched flag
M_FAULT_PRESENT   = M_BASE + 6   # 2054 - M6: Fault present flag
M_QTY_REACHED     = M_BASE + 7   # 2055 - M7: Quantity target reached flag
M_CUT_SOLENOID    = M_BASE + 8   # 2056 - M8: Cut solenoid energised flag
M_LENGTH_REACHED  = M_BASE + 9   # 2057 - M9: Length reached (internal)
M_AUTO_CYCLING    = M_BASE + 10  # 2058 - M10: Auto cycle running flag

# M Relays (Coils - HMI command bits)
M_CMD_AUTO_START  = M_BASE + 11  # 2059 - M11: HMI cmd: Request Auto Start
M_CMD_STOP        = M_BASE + 12  # 2060 - M12: HMI cmd: Request Stop
M_CMD_PUMP_ON     = M_BASE + 13  # 2061 - M13: HMI cmd: Request Pump ON
M_CMD_PUMP_OFF    = M_BASE + 14  # 2062 - M14: HMI cmd: Request Pump OFF
M_CMD_MANUAL_CUT  = M_BASE + 15  # 2063 - M15: HMI cmd: Request Manual Cut
M_CMD_CLEAR       = M_BASE + 16  # 2064 - M16: HMI cmd: Clear/reset counter
M_CMD_MODE_SET    = M_BASE + 17  # 2065 - M17: HMI cmd: Toggle Manual/Auto mode
M_CMD_JOG_FWD     = M_BASE + 20  # 2068 - M20: HMI cmd: Request FWD jog
M_CMD_JOG_BACK    = M_BASE + 21  # 2069 - M21: HMI cmd: Request BACK jog

# M Relays (Coils - alarm bits)
M_ALM_ESTOP       = M_BASE + 100  # 2148 - M100: Alarm: E-stop active
M_ALM_LOW_PRES    = M_BASE + 101  # 2149 - M101: Alarm: Hydraulic pressure low
M_ALM_ENCODER     = M_BASE + 102  # 2150 - M102: Alarm: Encoder fault
M_ALM_OVERTRAVEL  = M_BASE + 103  # 2151 - M103: Alarm: Overtravel limit
M_ALM_QTY_REACHED = M_BASE + 104  # 2152 - M104: Alarm: Quantity target reached


# === X Inputs (Discrete Inputs) - DVP-14ES has 8: X0-X7 ===
X_ENCODER_A       = X_BASE + 0   # 1024 - X0: Encoder channel A
X_ENCODER_B       = X_BASE + 1   # 1025 - X1: Encoder channel B
X_CUT_HOME        = X_BASE + 2   # 1026 - X2: Cut home/retract limit switch
X_MATERIAL_PRESENT = X_BASE + 3  # 1027 - X3: Material present sensor
X_PRESSURE_OK     = X_BASE + 4   # 1028 - X4: Hydraulic pressure switch OK (NC)
X_OVERTRAVEL      = X_BASE + 5   # 1029 - X5: Overtravel limit switch
X_GUARD_INTERLOCK = X_BASE + 6   # 1030 - X6: Guard/door interlock
X_ESTOP           = X_BASE + 7   # 1031 - X7: E-STOP (NC: 1=normal, 0=tripped)


# === Y Outputs (Coils) - DVP-14ES has 6: Y0-Y5 ===
Y_FEED_FWD        = Y_BASE + 0   # 1280 - Y0: Feed motor FWD contactor
Y_FEED_REV        = Y_BASE + 1   # 1281 - Y1: Feed motor REV contactor
Y_CUT_EXTEND      = Y_BASE + 2   # 1282 - Y2: Cut solenoid valve (extend)
Y_CUT_RETRACT     = Y_BASE + 3   # 1283 - Y3: Cut retract solenoid valve
Y_PUMP_MOTOR      = Y_BASE + 4   # 1284 - Y4: Hydraulic pump motor starter
Y_RUN_LAMP        = Y_BASE + 5   # 1285 - Y5: Run/fault indicator lamp


# === Address ranges for scanner/discovery ===
D_SCAN_RANGE = (D_BASE, D_BASE + 11)         # D0-D10
D_FAULT_RANGE = (D_BASE + 100, D_BASE + 103)  # D100-D102
M_STATUS_RANGE = (M_BASE, M_BASE + 22)        # M0-M21
M_ALARM_RANGE = (M_BASE + 100, M_BASE + 105)  # M100-M104
X_RANGE = (X_BASE, X_BASE + 8)                # X0-X7
Y_RANGE = (Y_BASE, Y_BASE + 6)                # Y0-Y5
