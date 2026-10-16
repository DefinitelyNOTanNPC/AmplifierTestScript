==============================================================================
README - Pico 2 PCB Bench Hardware Verification (main.py)
Source material for generating a Confluence page
==============================================================================

------------------------------------------------------------------------------
INSTRUCTIONS FOR CLAUDE (read first)
------------------------------------------------------------------------------
Using ONLY the reference information below, write the content for a
Confluence page that summarises the bench test script, the tests it runs and
the expected output of each test.

Requirements for the page:
- Audience: hardware/test engineers who may not have used a Pico or
  MicroPython before.
- Output in Confluence-friendly Markdown (headings, tables, bullet lists,
  code blocks), so it can be pasted into the Confluence editor.
- Suggested structure:
    1. Title: "Pico 2 PCB - Bench Hardware Verification"
    2. Overview (purpose, scope, NOT production firmware)
    3. Equipment required
    4. Pin map (table)
    5. Setup: flashing MicroPython and copying main.py (short)
    6. Using the menu (navigation keys, safe state)
    7. Test summary table: No. | Test | Type (Auto/Manual) | Jumpers/Setup |
       Pass criteria
    8. One section per test: purpose, setup, procedure, expected output
       (as a code block), failure diagnosis
    9. Recommended bench test order / checklist
   10. Troubleshooting
   11. Results record table (blank, for engineers to fill in per board:
       Board S/N | Date | Tester | Test 1..9 result | Notes)
- Use Confluence info/warning panel style for cautions (mark them clearly as
  "Warning:" or "Note:" so they can be converted to panels).
- Do not invent pins, values or behaviour not stated below. If something is
  unknown (e.g. FPGA pull-up rail voltage) write "[TBC]".

------------------------------------------------------------------------------
REFERENCE INFORMATION
------------------------------------------------------------------------------

1. OVERVIEW
- Board: custom PCB built around a Raspberry Pi Pico 2 (RP2350).
- Script: main.py, single file, MicroPython, no external libraries.
- Purpose: bench hardware verification only. Separate from production
  firmware.
- Runs automatically at power-up (MicroPython runs main.py on boot).
- Operated from a text menu over USB serial (REPL) on a PC, using Thonny or
  mpremote.

2. EQUIPMENT
- PC with USB cable (data-capable, not charge-only).
- Thonny IDE or mpremote (pip install mpremote).
- MicroPython firmware for Pico 2: https://micropython.org/download/RPI_PICO2/
- Multimeter (tests 2, 3, 7).
- Oscilloscope (optional; tests 5 and 8).
- 3 jumper wires: GP0-GP1, GP3-GP4, GP8-GP9.

3. PIN MAP (editable in the PINS dictionary at the top of main.py)
| Signal           | GPIO | Role in test script                       |
| UART_TX          | GP0  | UART0 TX (loopback test)                  |
| UART_RX          | GP1  | UART0 RX (loopback test)                  |
| SPI_CLK          | GP2  | SPI0 clock                                |
| SPI_MOSI         | GP3  | SPI0 MOSI                                 |
| SPI_MISO         | GP4  | SPI0 MISO                                 |
| SPI_CS           | GP5  | SPI0 chip select (driven LOW during test) |
| FPGA_CONF_GATE   | GP6  | Output, 2N7002 gate -> FPGA_CONF_PU       |
| FPGA_PS_EN_GATE  | GP13 | Output, 2N7002 gate -> FPGA_PS_EN_PU      |
| FPGA_PROG_GATE   | GP18 | Output, 2N7002 gate -> FPGA_PROG_PU       |
| FPGA_RST_GATE    | GP19 | Output, 2N7002 gate -> FPGA_RST_PU        |
| FPGA_PWR_GD      | GP14 | Input                                     |
| FPGA_FAIL        | GP15 | Input                                     |
| FPGA_INT         | GP17 | Input                                     |
| RB_CLK_LCK       | GP21 | Input                                     |
| 10M_1PPS_OUT     | GP16 | Configurable; 1 Hz square wave in test 8  |
| FPGA_RUN         | GP20 | Configurable (input/high-Z in this script)|
| 10M_1PPS_SIM     | GP22 | Configurable (input/high-Z in this script)|
| SPARE_8          | GP8  | Spare; GPIO loopback                      |
| SPARE_9          | GP9  | Spare; GPIO loopback                      |
| SPARE_10         | GP10 | Spare; not tested                         |
| ADC0             | GP26 | Analog input, 3.3 V reference             |
| ADC1             | GP27 | Analog input, 3.3 V reference             |
| ADC2             | GP28 | Analog input, 3.3 V reference             |
| Onboard LED      | GP25 ("LED") | Test 1 blink                      |

FET gate logic: each gate drives a 2N7002 N-FET with 10k gate pulldown.
Gate HIGH -> FET on -> FPGA_*_PU net pulled LOW (open-drain, inverted).
Gate LOW  -> FET off -> FPGA_*_PU net pulled HIGH by its pull-up.

4. SAFE STATE
Applied at boot, after every test (pass, fail, error or Ctrl-C) and on quit:
- FET gate pins (GP6, GP13, GP18, GP19): output LOW (FETs off, PU nets HIGH).
- All other pins: input / high-impedance (never driven, so cannot contend
  with the FPGA or a loopback jumper). UART and SPI peripherals released.
- Onboard LED off.

5. SETUP SUMMARY
Flash MicroPython (once):
  1. Hold BOOTSEL, plug in USB, release. Drive "RP2350" appears.
  2. Copy the RPI_PICO2 .uf2 onto the drive. Board reboots.
Copy and run with mpremote:
  mpremote cp main.py :main.py + reset + repl
  (Ctrl-] exits mpremote. Remove with: mpremote rm :main.py)
Copy with Thonny:
  Interpreter = MicroPython (Raspberry Pi Pico); File > Save as >
  Raspberry Pi Pico > main.py; Ctrl-D to restart.
Note: only one program (Thonny OR mpremote) can use the serial port at once.

6. MENU AND NAVIGATION
At boot: safe state -> test 1 runs once -> menu printed:
  --- MENU ---
   1. Board alive (blink LED)
   2. FET gate test (manual measure)
   3. Input monitor
   4. UART loopback (jumper GP0-GP1)
   5. SPI loopback (jumper MOSI-MISO)
   6. GPIO loopback (jumper GP8-GP9)
   7. ADC read
   8. 1 Hz square wave on GP16
   9. Run all automatic tests (4,5,6)
   q. Quit to REPL
  Select:
- Type number + Enter to run a test; menu reappears afterwards.
- Invalid input (or just Enter) prints "Invalid choice." and redraws menu.
- q + Enter: safe state, exit to MicroPython >>> prompt. Ctrl-D restarts.
- Ctrl-C during a test: prints "Aborted.", safe state, back to menu.
- Unexpected error: prints "Error: ...", safe state, back to menu.

7. TESTS

TEST 1 - Board alive (Manual/visual)
- Setup: none.
- Procedure: prints banner, blinks onboard LED 5 times (0.2 s on/0.2 s off).
- Expected output:
    ==============================
      Pico 2 PCB bench test
      Board alive - blinking LED
    ==============================
- Pass: banner shown and LED blinks 5 times.
- Fail: no text = USB/serial/firmware problem. Text but no blink = no LED on
  GP25 on this board (other tests unaffected).

TEST 2 - FET gate test (Manual, multimeter)
- Setup: board powered so FPGA_*_PU pull-ups have supply (rail [TBC]);
  multimeter DC volts, black probe to GND.
- Procedure: for each gate in order CONF (GP6), PS_EN (GP13), PROG (GP18),
  RST (GP19): gate set HIGH -> measure _PU net -> Enter; gate set LOW ->
  measure _PU net -> Enter.
- Expected output (per gate):
    FPGA_CONF_GATE (GP6) = HIGH -> measure FPGA_CONF_PU: expect LOW
    Press Enter to continue...
    FPGA_CONF_GATE (GP6) = LOW  -> measure FPGA_CONF_PU: expect HIGH
    Press Enter to continue...
  ... ends with "FET test done."
- Pass: _PU net ~0 V when gate HIGH; pull-up rail voltage when gate LOW.
  Gate pin itself ~3.3 V (HIGH) / 0 V (LOW).
- Diagnosis:
    HIGH step rail V, LOW step rail V -> FET not turning on (gate open,
      wrong pin, FET missing/bad joint).
    HIGH step ~0 V, LOW step ~0 V -> net stuck LOW (pull-up missing or
      unpowered, FET shorted, other load).
    Intermediate voltage -> check FET orientation / gate circuit.
- Result is judged by the operator; the script does not measure.

TEST 3 - Input monitor (Manual/observe)
- Setup: signals driven by FPGA side, or tie pins to 3.3 V/GND by hand.
- Procedure: prints input states every 0.5 s until Enter is pressed.
- Expected output (repeating line):
    FPGA_PWR_GD=1  FPGA_FAIL=0  FPGA_INT=0  RB_CLK_LCK=1
  (1 = HIGH, 0 = LOW)
- Pass: each column follows the real signal state when it is changed.
- Note: no internal pull-ups/pull-downs are enabled; an unconnected input
  floats and may show random/flickering 0/1. This is not a fault.

TEST 4 - UART loopback (Automatic)
- Setup: jumper GP0 (TX) to GP1 (RX).
- Procedure: UART0 115200 baud, flush, send "PICO2-UART-LOOPBACK-123",
  wait 0.1 s, read back, compare.
- Expected output (pass):
    UART loopback: sent b'PICO2-UART-LOOPBACK-123' got b'PICO2-UART-LOOPBACK-123' -> PASS
- Fail example: "... got b'' -> FAIL" = nothing received (jumper missing,
  wrong pins, open trace, line loaded). Garbled = noise/loading.

TEST 5 - SPI loopback (Automatic)
- Setup: jumper GP3 (MOSI) to GP4 (MISO).
- Procedure: SPI0 at 1 MHz; CS (GP5) LOW; transfer 8 bytes
  00 FF AA 55 01 80 3C C3 while reading MISO; CS HIGH; compare.
  Pattern detects stuck-low (00), stuck-high (FF), alternating bits (AA/55)
  and bit order/shift faults (01/80).
- Expected output (pass):
    SPI loopback: sent 00ffaa5501803cc3 got 00ffaa5501803cc3 -> PASS
- Fail: all 00 or all ff received = MISO not connected/stuck. Partially
  wrong or shifted = clock (GP2) or signal-integrity problem. CS can be
  used as a scope trigger.

TEST 6 - GPIO loopback (Automatic)
- Setup: jumper GP8 to GP9.
- Procedure: GP8 drives 1,0,1,0 (5 ms settle each), GP9 reads; then
  reversed (GP9 drives, GP8 reads).
- Expected output (pass):
    GPIO SPARE_8->SPARE_9: PASS
    GPIO SPARE_9->SPARE_8: PASS
- Fail: one direction only = one pin cannot drive or read (check joint).
  Both = missing jumper / open connection.
- GP10 not tested (change LOOP_A/LOOP_B in main.py to test it).

TEST 7 - ADC read (Manual, compare with multimeter)
- Setup: voltages on GP26/27/28. Warning: never exceed 3.3 V on ADC pins.
- Procedure: for ADC0, ADC1, ADC2: 10 samples, 50 ms apart.
- Expected output (example values):
    ADC0 (GP26):
       0: raw=31012  1.561 V
       1: raw=30998  1.561 V
       ...
- raw is 0-65535 (12-bit ADC scaled to 16 bits by MicroPython);
  voltage = raw x 3.3 / 65535 (VREF in main.py).
- Pass: agrees with multimeter to within a few tens of mV; samples stable.
- Note: unconnected ADC pins float and show wandering values.

TEST 8 - 1 Hz square wave on GP16 (Manual, scope/meter)
- Setup: scope or meter on GP16 (10M_1PPS_OUT).
- Procedure: GP16 output, 0.5 s HIGH / 0.5 s LOW x 10 (about 10 s), then
  back to input. Ctrl-C stops early.
- Expected output:
    1 Hz square wave on 10M_1PPS_OUT (GP16) for 10 s...
    Done.
- Pass: clean 0-3.3 V, 1 Hz, 50 % duty square wave for ~10 s.
- Warning: GP16 is actively driven during this test; ensure nothing else
  (e.g. the FPGA) drives the same net at the same time.

TEST 9 - Run all automatic tests (Automatic)
- Setup: all three jumpers fitted together: GP0-GP1, GP3-GP4, GP8-GP9.
- Procedure: runs tests 4, 5, 6 in sequence with safe state between each.
  An error in one test is recorded as FAIL and the rest still run.
- Expected output (all pass):
    ----- SUMMARY -----
      UART  PASS
      SPI   PASS
      GPIO  PASS
      OVERALL PASS
- Intended as the quick go/no-go check for each board.

8. RECOMMENDED BENCH ORDER
  1. Test 1 - board alive / connection OK
  2. Test 7 - ADC against meter
  3. Test 3 - FPGA status inputs
  4. Test 2 - FET gates with meter
  5. Fit 3 jumpers, Test 9 - all loopbacks
  6. Test 8 - GP16 on scope (if net used)
  7. Remove all jumpers before fitting the board into the system

9. TROUBLESHOOTING
| Symptom                         | Cause / fix                                    |
| No device found                 | Charge-only cable; still in BOOTSEL mode; MicroPython not flashed |
| Port busy / can't enter raw REPL| Close Thonny, serial monitors etc. (one program per port) |
| Permission denied (Linux)       | sudo usermod -aG dialout $USER, then re-login  |
| Blank screen after connecting   | Script waiting at "Select:" - press Enter or Ctrl-D |
| ImportError / pin errors        | Wrong firmware (must be Pico 2 / RP2350 build); or change Pin("LED") to Pin(25) |
| Script runs at every power-up   | Expected (main.py). Remove: mpremote rm :main.py |
