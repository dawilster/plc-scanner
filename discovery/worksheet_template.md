# Register Discovery Worksheet

**Date:** _______________
**PLC:** Delta DVP-14ES
**Connection:** Modbus TCP / RS-232 (circle one)
**Host/Port:** _______________

Instructions: Run the scanner, trigger each action in the console/on the machine,
and record which registers change. Fill in your best guess for what each register does.

---

## Holding Registers (D Registers - FC3)

| Address | Delta Symbol | Observed Value(s) | Behaviour When... | Your Label |
|---------|-------------|-------------------|-------------------|------------|
| 4096    | D0          |                   |                   |            |
| 4097    | D1          |                   |                   |            |
| 4098    | D2          |                   |                   |            |
| 4099    | D3          |                   |                   |            |
| 4100    | D4          |                   |                   |            |
| 4101    | D5          |                   |                   |            |
| 4102    | D6          |                   |                   |            |
| 4103    | D7          |                   |                   |            |
| 4104    | D8          |                   |                   |            |
| 4105    | D9          |                   |                   |            |
| 4106    | D10         |                   |                   |            |
| 4196    | D100        |                   |                   |            |
| 4197    | D101        |                   |                   |            |
| 4198    | D102        |                   |                   |            |

---

## Coils - M Relays (FC1)

| Address | Delta Symbol | State | Changes When... | Your Label |
|---------|-------------|-------|-----------------|------------|
| 2048    | M0          |       |                 |            |
| 2049    | M1          |       |                 |            |
| 2050    | M2          |       |                 |            |
| 2051    | M3          |       |                 |            |
| 2052    | M4          |       |                 |            |
| 2053    | M5          |       |                 |            |
| 2054    | M6          |       |                 |            |
| 2055    | M7          |       |                 |            |
| 2056    | M8          |       |                 |            |
| 2057    | M9          |       |                 |            |
| 2058    | M10         |       |                 |            |
| 2059    | M11         |       |                 |            |
| 2060    | M12         |       |                 |            |
| 2061    | M13         |       |                 |            |
| 2062    | M14         |       |                 |            |
| 2063    | M15         |       |                 |            |
| 2068    | M20         |       |                 |            |
| 2069    | M21         |       |                 |            |
| 2148    | M100        |       |                 |            |
| 2149    | M101        |       |                 |            |
| 2150    | M102        |       |                 |            |
| 2151    | M103        |       |                 |            |
| 2152    | M104        |       |                 |            |

---

## Coils - Y Outputs (FC1)

| Address | Delta Symbol | State | Changes When... | Your Label |
|---------|-------------|-------|-----------------|------------|
| 1280    | Y0          |       |                 |            |
| 1281    | Y1          |       |                 |            |
| 1282    | Y2          |       |                 |            |
| 1283    | Y3          |       |                 |            |
| 1284    | Y4          |       |                 |            |
| 1285    | Y5          |       |                 |            |

---

## Discrete Inputs - X Inputs (FC2)

| Address | Delta Symbol | State | Changes When... | Your Label |
|---------|-------------|-------|-----------------|------------|
| 1024    | X0          |       |                 |            |
| 1025    | X1          |       |                 |            |
| 1026    | X2          |       |                 |            |
| 1027    | X3          |       |                 |            |
| 1028    | X4          |       |                 |            |
| 1029    | X5          |       |                 |            |
| 1030    | X6          |       |                 |            |
| 1031    | X7          |       |                 |            |

---

## Observations / Sequence Notes

Use this section to record what happens during a full auto cycle:

1. Before start:
   -

2. After pressing Start:
   -

3. During feed (material moving):
   -

4. When length is reached:
   -

5. During cut:
   -

6. After cut completes:
   -

7. When quantity target reached:
   -

8. E-Stop test:
   -

---

## Your Conclusions

Based on your observations, what is:

- The machine state register? D___
- The length measurement register? D___
- The length setpoint register? D___
- The piece counter register? D___
- The quantity target register? D___
- The pump running status? M___
- The auto mode flag? M___
- The feed motor output? Y___
- The cut solenoid output? Y___
- The pump motor output? Y___
- The E-stop input? X___

Compare your answers against `docs/register_map.md` when done.
