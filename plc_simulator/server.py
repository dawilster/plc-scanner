"""
Modbus TCP Server - simulates a Delta DVP-14ES PLC

Uses ModbusSparseDataBlock to match the non-contiguous Delta DVP address space.
Port 5020 by default (no root needed). On-site Delta DVP uses port 502.

NOTE: pymodbus 3.12+ renamed ModbusSlaveContext -> ModbusDeviceContext
and ModbusDeviceContext adds +1 to all addresses internally.
pymodbus <3.12 uses ModbusSlaveContext which also adds +1.
So we store values at (address + 1) in the sparse datablock.
"""

import pymodbus
from pymodbus.datastore import ModbusServerContext, ModbusSparseDataBlock
from pymodbus.server import StartAsyncTcpServer

_PV = tuple(int(x) for x in pymodbus.__version__.split(".")[:2])
if _PV >= (3, 12):
    from pymodbus.datastore import ModbusDeviceContext as _SlaveContext
    _CTX_KW = "devices"
else:
    from pymodbus.datastore import ModbusSlaveContext as _SlaveContext
    _CTX_KW = "slaves"

from .registers import (
    D_BASE, M_BASE, X_BASE, Y_BASE,
    X_ESTOP, X_PRESSURE_OK, X_CUT_HOME, X_MATERIAL_PRESENT, X_GUARD_INTERLOCK,
)

# Offset to compensate for internal address += 1
_OFFSET = 1


def build_datastore():
    """Build the Modbus datastore with Delta DVP address mapping."""

    # Holding registers: D0-D10 and D100-D102
    hr = {}
    for i in range(11):
        hr[D_BASE + i + _OFFSET] = 0
    for i in range(100, 103):
        hr[D_BASE + i + _OFFSET] = 0

    # Coils: M relays (M0-M21, M100-M104) + Y outputs (Y0-Y5)
    co = {}
    for i in range(22):
        co[M_BASE + i + _OFFSET] = 0
    for i in range(100, 105):
        co[M_BASE + i + _OFFSET] = 0
    for i in range(6):
        co[Y_BASE + i + _OFFSET] = 0

    # Discrete inputs: X0-X7
    di = {}
    for i in range(8):
        di[X_BASE + i + _OFFSET] = 0
    # NC contacts default to 1 (healthy/normal state)
    di[X_ESTOP + _OFFSET] = 1           # E-stop not tripped
    di[X_PRESSURE_OK + _OFFSET] = 1     # Pressure OK
    di[X_CUT_HOME + _OFFSET] = 1        # Cut cylinder at home
    di[X_MATERIAL_PRESENT + _OFFSET] = 1  # Material loaded
    di[X_GUARD_INTERLOCK + _OFFSET] = 1   # Guard closed

    # Input registers (not commonly used in DVP, placeholder)
    ir = {0: 0}

    slave = _SlaveContext(
        di=ModbusSparseDataBlock(di),
        co=ModbusSparseDataBlock(co),
        ir=ModbusSparseDataBlock(ir),
        hr=ModbusSparseDataBlock(hr),
    )

    return ModbusServerContext(**{_CTX_KW: slave}, single=True)


async def run_server(context, host="0.0.0.0", port=5020):
    """Start the Modbus TCP server."""
    await StartAsyncTcpServer(
        context=context,
        address=(host, port),
    )
