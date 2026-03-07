"""
Roll Form Cut-to-Length Machine State Machine

Simulates a Delta DVP-14ES controlling:
- Feed rollers (motor FWD/REV via VFD)
- Hydraulic cut-off press (extend/retract solenoids)
- Hydraulic pump
- Encoder for length measurement
- Piece counter

Runs at 100ms tick rate. Reads HMI command bits (M11-M21) and sensor inputs (X0-X7),
drives outputs (Y0-Y5) and updates status registers (D0-D10, M0-M10).
"""

import asyncio
import time

from .registers import *


class RollFormMachine:
    def __init__(self, context):
        self.ctx = context
        self.tick_s = 0.1  # 100ms tick
        self.cut_timer = 0.0
        self.cut_phase = 0  # 0=not cutting, 1=extending, 2=dwelling, 3=retracting
        self.pressure_ramp_time = 0.0
        self.pump_start_time = 0.0

    # --- Datastore Accessors ---
    # The slave context is at address 0x00 (single=True mode)

    def _get_hr(self, addr):
        """Read a holding register."""
        return self.ctx[0x00].getValues(3, addr, count=1)[0]

    def _set_hr(self, addr, val):
        """Write a holding register."""
        self.ctx[0x00].setValues(3, addr, [int(val)])

    def _get_coil(self, addr):
        """Read a coil (M relay or Y output)."""
        return bool(self.ctx[0x00].getValues(1, addr, count=1)[0])

    def _set_coil(self, addr, val):
        """Write a coil (M relay or Y output)."""
        self.ctx[0x00].setValues(1, addr, [bool(val)])

    def _get_di(self, addr):
        """Read a discrete input (X input)."""
        return bool(self.ctx[0x00].getValues(2, addr, count=1)[0])

    def _set_di(self, addr, val):
        """Write a discrete input (for simulation)."""
        self.ctx[0x00].setValues(2, addr, [bool(val)])

    # --- Convenience ---

    @property
    def state(self):
        return self._get_hr(D_MACHINE_STATE)

    @state.setter
    def state(self, val):
        self._set_hr(D_MACHINE_STATE, val)

    def _all_outputs_off(self):
        """Kill all Y outputs immediately."""
        for addr in [Y_FEED_FWD, Y_FEED_REV, Y_CUT_EXTEND, Y_CUT_RETRACT, Y_PUMP_MOTOR, Y_RUN_LAMP]:
            self._set_coil(addr, False)

    def _clear_motion_flags(self):
        """Clear motion status M relays."""
        self._set_coil(M_FEED_FWD, False)
        self._set_coil(M_FEED_REV, False)
        self._set_coil(M_CUTTING, False)
        self._set_coil(M_CUT_SOLENOID, False)

    def _push_fault_history(self, code):
        """Push a fault code into the history ring buffer."""
        if code == FAULT_NONE:
            return
        h1 = self._get_hr(D_FAULT_HIST_0)
        h2 = self._get_hr(D_FAULT_HIST_1)
        self._set_hr(D_FAULT_HIST_2, h2)
        self._set_hr(D_FAULT_HIST_1, h1)
        self._set_hr(D_FAULT_HIST_0, code)

    # --- Main Loop ---

    async def run(self):
        """Main simulation loop."""
        while True:
            await asyncio.sleep(self.tick_s)
            try:
                self._tick()
            except Exception as e:
                print(f"Machine tick error: {e}")

    def _tick(self):
        """Single simulation tick."""
        # Always check E-stop first (highest priority)
        if self._check_estop():
            return

        # Process HMI command bits
        self._process_commands()

        # Run state-specific logic
        state = self.state
        if state == STATE_IDLE:
            self._tick_idle()
        elif state == STATE_RUNNING:
            self._tick_running()
        elif state == STATE_CUTTING:
            self._tick_cutting()
        elif state == STATE_FAULT:
            self._tick_fault()

        # Update hydraulic pressure simulation
        self._update_pressure()

        # Update output coils from internal state
        self._update_outputs()

        # Update alarm bits
        self._update_alarms()

    # --- E-Stop Check ---

    def _check_estop(self):
        """Check E-stop input. Returns True if E-stopped (skip other processing)."""
        estop_tripped = not self._get_di(X_ESTOP)  # NC: 0 = tripped

        if estop_tripped and self.state != STATE_ESTOP:
            # Enter E-stop state
            self._all_outputs_off()
            self._clear_motion_flags()
            self.state = STATE_ESTOP
            self._set_coil(M_ESTOP_LATCHED, True)
            self._set_hr(D_FAULT_CODE, FAULT_ESTOP)
            self._set_coil(M_FAULT_PRESENT, True)
            self._push_fault_history(FAULT_ESTOP)
            self.cut_phase = 0
            return True

        if self.state == STATE_ESTOP:
            # Stay in E-stop until reset
            self._all_outputs_off()
            estop_normal = self._get_di(X_ESTOP)  # E-stop released
            stop_cmd = self._get_coil(M_CMD_STOP)

            if estop_normal and stop_cmd:
                # Clear E-stop
                self._set_coil(M_CMD_STOP, False)
                self._set_coil(M_ESTOP_LATCHED, False)
                self._set_hr(D_FAULT_CODE, FAULT_NONE)
                self._set_coil(M_FAULT_PRESENT, False)
                self.state = STATE_IDLE
            return True

        return False

    # --- Command Processing ---

    def _process_commands(self):
        """Read and process HMI command bits (M11-M21). Clear them after processing."""

        # Stop command - always processed
        if self._get_coil(M_CMD_STOP):
            self._set_coil(M_CMD_STOP, False)
            if self.state in (STATE_RUNNING, STATE_CUTTING):
                self._all_outputs_off()
                self._clear_motion_flags()
                self._set_coil(M_AUTO_CYCLING, False)
                self.cut_phase = 0
                self.state = STATE_IDLE

            # If in fault state, stop acts as reset
            if self.state == STATE_FAULT:
                fault = self._get_hr(D_FAULT_CODE)
                if self._is_fault_cleared(fault):
                    self._set_hr(D_FAULT_CODE, FAULT_NONE)
                    self._set_coil(M_FAULT_PRESENT, False)
                    self.state = STATE_IDLE

        # Pump ON
        if self._get_coil(M_CMD_PUMP_ON):
            self._set_coil(M_CMD_PUMP_ON, False)
            if not self._get_coil(M_PUMP_RUNNING):
                self._set_coil(M_PUMP_RUNNING, True)
                self._set_coil(Y_PUMP_MOTOR, True)
                self.pump_start_time = time.monotonic()

        # Pump OFF
        if self._get_coil(M_CMD_PUMP_OFF):
            self._set_coil(M_CMD_PUMP_OFF, False)
            self._set_coil(M_PUMP_RUNNING, False)
            self._set_coil(Y_PUMP_MOTOR, False)

        # Auto Start
        if self._get_coil(M_CMD_AUTO_START):
            self._set_coil(M_CMD_AUTO_START, False)
            if (self.state == STATE_IDLE
                    and self._get_hr(D_MODE) == 1  # Must be in Auto mode
                    and self._get_coil(M_PUMP_RUNNING)):  # Pump must be running
                self._set_hr(D_CURRENT_LENGTH, 0)
                self._set_coil(M_AUTO_ACTIVE, True)
                self._set_coil(M_AUTO_CYCLING, True)
                self._set_coil(M_QTY_REACHED, False)
                self.state = STATE_RUNNING

        # Manual Cut
        if self._get_coil(M_CMD_MANUAL_CUT):
            self._set_coil(M_CMD_MANUAL_CUT, False)
            if (self.state == STATE_IDLE
                    and self._get_hr(D_MODE) == 0  # Manual mode only
                    and self._get_coil(M_PUMP_RUNNING)):
                self.state = STATE_CUTTING
                self.cut_phase = 1
                self.cut_timer = 0.0

        # Clear - reset piece counter (only when idle)
        if self._get_coil(M_CMD_CLEAR):
            self._set_coil(M_CMD_CLEAR, False)
            if self.state == STATE_IDLE:
                self._set_hr(D_QTY_CURRENT, 0)
                self._set_coil(M_QTY_REACHED, False)

        # Mode Set - toggle Manual/Auto (only when idle)
        if self._get_coil(M_CMD_MODE_SET):
            self._set_coil(M_CMD_MODE_SET, False)
            if self.state == STATE_IDLE:
                current = self._get_hr(D_MODE)
                new_mode = 0 if current == 1 else 1
                self._set_hr(D_MODE, new_mode)

    def _is_fault_cleared(self, fault_code):
        """Check if the condition that caused the fault has been resolved."""
        if fault_code == FAULT_LOW_PRES:
            return self._get_hr(D_HYD_PRESSURE) >= 30
        if fault_code == FAULT_OVERTRAVEL:
            return not self._get_di(X_OVERTRAVEL)
        return True

    # --- State Ticks ---

    def _tick_idle(self):
        """IDLE state - handle jog commands."""
        self._set_coil(M_AUTO_ACTIVE, False)
        self._set_coil(M_AUTO_CYCLING, False)

        # Jog forward (momentary)
        jog_fwd = self._get_coil(M_CMD_JOG_FWD)
        if jog_fwd:
            self._set_coil(M_CMD_JOG_FWD, False)
            self._set_coil(M_FEED_FWD, True)
            self._set_coil(Y_FEED_FWD, True)
            # Advance encoder a small amount per jog press
            length = self._get_hr(D_CURRENT_LENGTH)
            speed = max(self._get_hr(D_FEED_SPEED), 10)
            self._set_hr(D_CURRENT_LENGTH, length + int(speed * self.tick_s))
        else:
            self._set_coil(M_FEED_FWD, False)
            self._set_coil(Y_FEED_FWD, False)

        # Jog reverse (momentary)
        jog_back = self._get_coil(M_CMD_JOG_BACK)
        if jog_back:
            self._set_coil(M_CMD_JOG_BACK, False)
            self._set_coil(M_FEED_REV, True)
            self._set_coil(Y_FEED_REV, True)
            length = self._get_hr(D_CURRENT_LENGTH)
            self._set_hr(D_CURRENT_LENGTH, max(0, length - int(max(self._get_hr(D_FEED_SPEED), 10) * self.tick_s)))
        else:
            self._set_coil(M_FEED_REV, False)
            self._set_coil(Y_FEED_REV, False)

    def _tick_running(self):
        """RUNNING state - feed material, check length."""
        self._set_coil(M_FEED_FWD, True)
        self._set_coil(Y_FEED_FWD, True)

        # Advance encoder
        speed = max(self._get_hr(D_FEED_SPEED), 10)  # mm/sec, minimum 10
        increment = speed * self.tick_s  # mm per tick
        current = self._get_hr(D_CURRENT_LENGTH)
        new_length = current + int(increment)
        self._set_hr(D_CURRENT_LENGTH, new_length)

        # Check if target length reached
        setpoint = self._get_hr(D_LENGTH_SETPOINT)
        if setpoint > 0 and new_length >= setpoint:
            self._set_hr(D_CURRENT_LENGTH, setpoint)  # Clamp to exact setpoint
            self._set_coil(M_LENGTH_REACHED, True)
            self._set_coil(M_FEED_FWD, False)
            self._set_coil(Y_FEED_FWD, False)
            self.state = STATE_CUTTING
            self.cut_phase = 1
            self.cut_timer = 0.0

        # Check for faults during run
        self._check_run_faults()

    def _tick_cutting(self):
        """CUTTING state - extend, dwell, retract, count."""
        dwell_time = max(self._get_hr(D_CUT_DWELL), 100) / 1000.0  # Convert ms to seconds
        retract_time = 0.3  # Fixed 300ms retract

        self.cut_timer += self.tick_s

        if self.cut_phase == 1:
            # Phase 1: Extend cut solenoid
            self._set_coil(M_CUTTING, True)
            self._set_coil(M_CUT_SOLENOID, True)
            self._set_coil(Y_CUT_EXTEND, True)
            self._set_coil(Y_CUT_RETRACT, False)
            if self.cut_timer >= 0.2:  # 200ms to extend
                self.cut_phase = 2
                self.cut_timer = 0.0

        elif self.cut_phase == 2:
            # Phase 2: Dwell at bottom
            if self.cut_timer >= dwell_time:
                self.cut_phase = 3
                self.cut_timer = 0.0

        elif self.cut_phase == 3:
            # Phase 3: Retract
            self._set_coil(Y_CUT_EXTEND, False)
            self._set_coil(Y_CUT_RETRACT, True)
            if self.cut_timer >= retract_time:
                # Cut complete
                self._set_coil(Y_CUT_RETRACT, False)
                self._set_coil(M_CUTTING, False)
                self._set_coil(M_CUT_SOLENOID, False)
                self._set_coil(M_LENGTH_REACHED, False)
                self.cut_phase = 0

                # Increment piece counter
                count = self._get_hr(D_QTY_CURRENT) + 1
                self._set_hr(D_QTY_CURRENT, count)

                # Reset encoder
                self._set_hr(D_CURRENT_LENGTH, 0)

                # Check if quantity target reached
                target = self._get_hr(D_QTY_TARGET)
                if target > 0 and count >= target:
                    self._set_coil(M_QTY_REACHED, True)
                    self._set_coil(M_AUTO_CYCLING, False)
                    self._set_coil(M_AUTO_ACTIVE, False)
                    self.state = STATE_IDLE
                elif self._get_coil(M_AUTO_CYCLING):
                    # Continue auto cycle - next piece
                    self.state = STATE_RUNNING
                else:
                    self.state = STATE_IDLE

    def _tick_fault(self):
        """FAULT state - outputs off, wait for reset."""
        self._set_coil(Y_FEED_FWD, False)
        self._set_coil(Y_FEED_REV, False)
        self._set_coil(Y_CUT_EXTEND, False)
        self._set_coil(Y_CUT_RETRACT, False)
        self._clear_motion_flags()
        # Pump stays in whatever state it was in
        # Fault lamp handled in _update_outputs

    # --- Physics Simulation ---

    def _update_pressure(self):
        """Simulate hydraulic pressure based on pump state."""
        if self._get_coil(M_PUMP_RUNNING):
            elapsed = time.monotonic() - self.pump_start_time
            # Ramp from 0 to 150 bar over ~2 seconds
            target = 150
            if elapsed < 2.0:
                pressure = int(target * (elapsed / 2.0))
            else:
                pressure = target

            # Spike during cutting
            if self._get_coil(M_CUTTING):
                pressure = 180

            self._set_hr(D_HYD_PRESSURE, pressure)

            # Low pressure fault check (after initial ramp period)
            if elapsed > 3.0 and pressure < 30:
                self._enter_fault(FAULT_LOW_PRES)
        else:
            # Pressure decays when pump off
            current = self._get_hr(D_HYD_PRESSURE)
            if current > 0:
                self._set_hr(D_HYD_PRESSURE, max(0, current - 5))

    def _check_run_faults(self):
        """Check for faults during operation."""
        # Overtravel check
        if self._get_di(X_OVERTRAVEL):
            self._enter_fault(FAULT_OVERTRAVEL)

        # Low pressure during operation
        if self._get_coil(M_PUMP_RUNNING) and self._get_hr(D_HYD_PRESSURE) < 30:
            elapsed = time.monotonic() - self.pump_start_time
            if elapsed > 3.0:
                self._enter_fault(FAULT_LOW_PRES)

    def _enter_fault(self, code):
        """Transition to FAULT state."""
        self._all_outputs_off()
        self._clear_motion_flags()
        self._set_coil(M_AUTO_CYCLING, False)
        self.cut_phase = 0
        self.state = STATE_FAULT
        self._set_hr(D_FAULT_CODE, code)
        self._set_coil(M_FAULT_PRESENT, True)
        self._push_fault_history(code)
        # Keep pump state - operator may want it running

    # --- Output Updates ---

    def _update_outputs(self):
        """Update indicator lamp based on state."""
        state = self.state
        # Y5 = run lamp: ON when running or cutting, BLINK when fault (simulated as ON)
        if state in (STATE_RUNNING, STATE_CUTTING):
            self._set_coil(Y_RUN_LAMP, True)
        elif state in (STATE_FAULT, STATE_ESTOP):
            # In real life this would blink - we just keep it on
            self._set_coil(Y_RUN_LAMP, True)
        else:
            self._set_coil(Y_RUN_LAMP, False)

    def _update_alarms(self):
        """Update alarm M relays from current state."""
        self._set_coil(M_ALM_ESTOP, self.state == STATE_ESTOP)
        self._set_coil(M_ALM_LOW_PRES, self._get_hr(D_FAULT_CODE) == FAULT_LOW_PRES)
        self._set_coil(M_ALM_ENCODER, self._get_hr(D_FAULT_CODE) == FAULT_ENCODER)
        self._set_coil(M_ALM_OVERTRAVEL, self._get_hr(D_FAULT_CODE) == FAULT_OVERTRAVEL)
        self._set_coil(M_ALM_QTY_REACHED, self._get_coil(M_QTY_REACHED))
