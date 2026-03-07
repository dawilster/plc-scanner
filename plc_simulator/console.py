"""
Keyboard Console - simulates operator panel and sensor events

Operator keys (write M relay commands, like the TG765 HMI would):
  s = Auto Start (M11)    x = Stop/Reset (M12)
  p = Pump ON (M13)       o = Pump OFF (M14)
  c = Manual Cut (M15)    f = Jog FWD (M20)
  b = Jog BACK (M21)      a = Mode Set (M17)
  r = Clear counter (M16)  +/- = Speed up/down

Sensor/event keys (toggle X inputs):
  e = Toggle E-STOP (X7)
  m = Toggle material present (X3)
  h = Toggle hydraulic pressure OK (X4)
  t = Toggle overtravel limit (X5)

  q = Quit
"""

import asyncio
import select
import sys
import termios
import tty

from .registers import *


class Console:
    def __init__(self, context):
        self.ctx = context
        self._old_settings = None

    def _set_coil(self, addr, val):
        self.ctx[0x00].setValues(1, addr, [bool(val)])

    def _get_coil(self, addr):
        return bool(self.ctx[0x00].getValues(1, addr, count=1)[0])

    def _set_hr(self, addr, val):
        self.ctx[0x00].setValues(3, addr, [int(val)])

    def _get_hr(self, addr):
        return self.ctx[0x00].getValues(3, addr, count=1)[0]

    def _get_di(self, addr):
        return bool(self.ctx[0x00].getValues(2, addr, count=1)[0])

    def _set_di(self, addr, val):
        self.ctx[0x00].setValues(2, addr, [bool(val)])

    def _print_status(self):
        state = self._get_hr(D_MACHINE_STATE)
        state_name = STATE_NAMES.get(state, "???")
        mode = "AUTO" if self._get_hr(D_MODE) == 1 else "MANUAL"
        length = self._get_hr(D_CURRENT_LENGTH)
        setpoint = self._get_hr(D_LENGTH_SETPOINT)
        qty = self._get_hr(D_QTY_CURRENT)
        qty_target = self._get_hr(D_QTY_TARGET)
        pressure = self._get_hr(D_HYD_PRESSURE)
        pump = "ON" if self._get_coil(M_PUMP_RUNNING) else "OFF"
        fault = self._get_hr(D_FAULT_CODE)
        fault_name = FAULT_NAMES.get(fault, "???")
        estop = "TRIPPED" if not self._get_di(X_ESTOP) else "OK"

        print(f"\r\033[K  [{state_name}] {mode} | "
              f"Length: {length}/{setpoint}mm | "
              f"Qty: {qty}/{qty_target} | "
              f"Pressure: {pressure}bar | "
              f"Pump: {pump} | "
              f"Fault: {fault_name} | "
              f"E-Stop: {estop}", end="", flush=True)

    async def run(self):
        """Read keyboard input and map to PLC actions."""
        print("\n=== OPERATOR CONSOLE ===")
        print("Operator:  s=Start  x=Stop  p=PumpON  o=PumpOFF  c=Cut  f=FWD  b=BACK")
        print("           a=Mode   r=Clear  +=SpeedUp  -=SlowDown  y=SysSet")
        print("Sensors:   e=E-Stop  m=Material  h=Pressure  t=Overtravel")
        print("           q=Quit")
        print("=" * 80)

        # Set terminal to raw mode for single keypress reading
        fd = sys.stdin.fileno()
        self._old_settings = termios.tcgetattr(fd)

        try:
            tty.setcbreak(fd)
            while True:
                self._print_status()
                # Non-blocking check with select, then sleep to yield to event loop
                if select.select([sys.stdin], [], [], 0)[0]:
                    char = sys.stdin.read(1)
                    if char == 'q':
                        print("\n\nShutting down...")
                        break
                    self._handle_key(char)
                else:
                    await asyncio.sleep(0.05)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, self._old_settings)

    def _handle_key(self, key):
        """Map keypress to PLC action."""
        key = key.lower()

        # Operator commands (set M relay command bits)
        if key == 's':
            self._set_coil(M_CMD_AUTO_START, True)
            print(f"\n  >> AUTO START (M11)")
        elif key == 'x':
            self._set_coil(M_CMD_STOP, True)
            print(f"\n  >> STOP/RESET (M12)")
        elif key == 'p':
            self._set_coil(M_CMD_PUMP_ON, True)
            print(f"\n  >> PUMP ON (M13)")
        elif key == 'o':
            self._set_coil(M_CMD_PUMP_OFF, True)
            print(f"\n  >> PUMP OFF (M14)")
        elif key == 'c':
            self._set_coil(M_CMD_MANUAL_CUT, True)
            print(f"\n  >> MANUAL CUT (M15)")
        elif key == 'f':
            self._set_coil(M_CMD_JOG_FWD, True)
            print(f"\n  >> JOG FWD (M20)")
        elif key == 'b':
            self._set_coil(M_CMD_JOG_BACK, True)
            print(f"\n  >> JOG BACK (M21)")
        elif key == 'a':
            self._set_coil(M_CMD_MODE_SET, True)
            current = self._get_hr(D_MODE)
            print(f"\n  >> MODE SET (M17) — currently {'AUTO' if current == 1 else 'MANUAL'}")
        elif key == 'r':
            self._set_coil(M_CMD_CLEAR, True)
            print(f"\n  >> CLEAR (M16)")
        elif key == '+':
            current = self._get_hr(D_FEED_SPEED)
            new_speed = min(current + 50, 500)
            self._set_hr(D_FEED_SPEED, new_speed)
            print(f"\n  >> SPEED UP: {new_speed}mm/s")
        elif key == '-':
            current = self._get_hr(D_FEED_SPEED)
            new_speed = max(current - 50, 10)
            self._set_hr(D_FEED_SPEED, new_speed)
            print(f"\n  >> SLOW DOWN: {new_speed}mm/s")

        # Sensor toggles (toggle X discrete inputs)
        elif key == 'e':
            current = self._get_di(X_ESTOP)
            self._set_di(X_ESTOP, not current)
            print(f"\n  >> E-STOP {'RELEASED' if not current else 'TRIPPED'} (X7={'1' if not current else '0'})")
        elif key == 'm':
            current = self._get_di(X_MATERIAL_PRESENT)
            self._set_di(X_MATERIAL_PRESENT, not current)
            print(f"\n  >> MATERIAL {'PRESENT' if not current else 'ABSENT'} (X3={'1' if not current else '0'})")
        elif key == 'h':
            current = self._get_di(X_PRESSURE_OK)
            self._set_di(X_PRESSURE_OK, not current)
            print(f"\n  >> PRESSURE SWITCH {'OK' if not current else 'FAIL'} (X4={'1' if not current else '0'})")
        elif key == 't':
            current = self._get_di(X_OVERTRAVEL)
            self._set_di(X_OVERTRAVEL, not current)
            print(f"\n  >> OVERTRAVEL {'ACTIVE' if not current else 'CLEAR'} (X5={'1' if not current else '0'})")

        elif key == 'y':
            self._sys_set()

    def _sys_set(self):
        """SYS SET screen — edit machine setpoints interactively."""
        fd = sys.stdin.fileno()
        # Restore normal terminal mode for line input
        termios.tcsetattr(fd, termios.TCSADRAIN, self._old_settings)

        params = [
            ("Length Setpoint (mm)", D_LENGTH_SETPOINT),
            ("Quantity Target",      D_QTY_TARGET),
            ("Feed Speed (mm/s)",    D_FEED_SPEED),
            ("Cut Dwell (ms)",       D_CUT_DWELL),
            ("Encoder Cal (p/mm)",   D_ENCODER_CAL),
        ]

        print("\n")
        print("=" * 50)
        print("  SYS SET — Machine Parameters")
        print("=" * 50)
        for i, (label, addr) in enumerate(params, 1):
            val = self._get_hr(addr)
            print(f"  {i}. {label:.<30} {val}")
        print(f"  0. Back")
        print("=" * 50)

        try:
            choice = input("  Select parameter [0-5]: ").strip()
            if choice in ('0', ''):
                print("  (no changes)")
            elif choice in ('1', '2', '3', '4', '5'):
                idx = int(choice) - 1
                label, addr = params[idx]
                current = self._get_hr(addr)
                new_val = input(f"  {label} [{current}] -> ").strip()
                if new_val == '':
                    print("  (no change)")
                else:
                    try:
                        val = int(new_val)
                        if val < 0 or val > 65535:
                            print("  !! Value must be 0-65535")
                        else:
                            self._set_hr(addr, val)
                            print(f"  >> Set {label} = {val}")
                    except ValueError:
                        print("  !! Invalid number")
            else:
                print("  !! Invalid choice")
        except (EOFError, KeyboardInterrupt):
            print("\n  (cancelled)")

        print()
        # Restore cbreak mode
        tty.setcbreak(fd)
