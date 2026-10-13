# Pico 2 (RP2350) custom PCB - bench hardware verification.
# Not production firmware. MicroPython, no external libraries.
# Run, then use the menu in the REPL (Thonny shell or `mpremote`).

from machine import Pin, UART, SPI, ADC
import time

# ---------------------------------------------------------------- pin map ---
PINS = {
    # UART0
    "UART_TX": 0, "UART_RX": 1,
    # SPI0
    "SPI_CLK": 2, "SPI_MOSI": 3, "SPI_MISO": 4, "SPI_CS": 5,
    # FET gate outputs (HIGH = FET on = FPGA_*_PU net pulled LOW)
    "FPGA_CONF_GATE": 6, "FPGA_PS_EN_GATE": 13,
    "FPGA_PROG_GATE": 18, "FPGA_RST_GATE": 19,
    # Inputs
    "FPGA_PWR_GD": 14, "FPGA_FAIL": 15, "FPGA_INT": 17, "RB_CLK_LCK": 21,
    # Configurable
    "FPGA_RUN": 20, "10M_1PPS_OUT": 16, "10M_1PPS_SIM": 22,
    # Spare
    "SPARE_8": 8, "SPARE_9": 9, "SPARE_10": 10,
    # ADC (3.3 V ref)
    "ADC0": 26, "ADC1": 27, "ADC2": 28,
}

GATES = ["FPGA_CONF_GATE", "FPGA_PS_EN_GATE", "FPGA_PROG_GATE", "FPGA_RST_GATE"]
INPUTS = ["FPGA_PWR_GD", "FPGA_FAIL", "FPGA_INT", "RB_CLK_LCK"]
CONFIGURABLE = ["FPGA_RUN", "10M_1PPS_OUT", "10M_1PPS_SIM"]
SPARE = ["SPARE_8", "SPARE_9", "SPARE_10"]
LOOP_A, LOOP_B = "SPARE_8", "SPARE_9"   # GPIO loopback jumper
SQUARE_PIN = "10M_1PPS_OUT"
VREF = 3.3

led = Pin("LED", Pin.OUT)


def p(name):
    return PINS[name]


# ------------------------------------------------------------- safe state ---
def safe_state():
    """FET gates driven LOW (FETs off). Everything else high-Z input so we
    never fight the FPGA or a jumper. UART/SPI pins released to inputs."""
    for n in GATES:
        Pin(p(n), Pin.OUT, value=0)
    for n in INPUTS + CONFIGURABLE + SPARE:
        Pin(p(n), Pin.IN)
    for n in ("UART_TX", "UART_RX", "SPI_CLK", "SPI_MOSI", "SPI_MISO", "SPI_CS"):
        Pin(p(n), Pin.IN)
    led.value(0)


def wait_enter(msg="Press Enter to continue..."):
    input(msg)


# ------------------------------------------------------------------ tests ---
def test_alive():
    print("\n==============================")
    print("  Pico 2 PCB bench test")
    print("  Board alive - blinking LED")
    print("==============================")
    for _ in range(5):
        led.value(1); time.sleep(0.2)
        led.value(0); time.sleep(0.2)


def test_fets():
    for n in GATES:
        g = Pin(p(n), Pin.OUT, value=1)
        net = n.replace("_GATE", "_PU")
        print("\n{} (GP{}) = HIGH -> measure {}: expect LOW".format(n, p(n), net))
        wait_enter()
        g.value(0)
        print("{} (GP{}) = LOW  -> measure {}: expect HIGH".format(n, p(n), net))
        wait_enter()
    print("FET test done.")


def test_inputs():
    import select, sys
    pins = {n: Pin(p(n), Pin.IN) for n in INPUTS}
    print("Monitoring inputs, press Enter to stop.")
    poll = select.poll()
    poll.register(sys.stdin, select.POLLIN)
    while True:
        print("  ".join("{}={}".format(n, pins[n].value()) for n in INPUTS))
        if poll.poll(500):
            sys.stdin.readline()
            break


def test_uart():
    msg = b"PICO2-UART-LOOPBACK-123"
    u = UART(0, baudrate=115200, tx=Pin(p("UART_TX")), rx=Pin(p("UART_RX")),
             timeout=200)
    while u.any():
        u.read()                      # flush
    u.write(msg)
    time.sleep(0.1)
    got = u.read(len(msg)) or b""
    u.deinit()
    ok = got == msg
    print("UART loopback: sent {} got {} -> {}".format(msg, got, "PASS" if ok else "FAIL"))
    return ok


def test_spi():
    tx = bytes([0x00, 0xFF, 0xAA, 0x55, 0x01, 0x80, 0x3C, 0xC3])
    rx = bytearray(len(tx))
    cs = Pin(p("SPI_CS"), Pin.OUT, value=1)
    s = SPI(0, baudrate=1_000_000, sck=Pin(p("SPI_CLK")),
            mosi=Pin(p("SPI_MOSI")), miso=Pin(p("SPI_MISO")))
    cs.value(0)
    s.write_readinto(tx, rx)
    cs.value(1)
    s.deinit()
    ok = bytes(rx) == tx
    print("SPI loopback: sent {} got {} -> {}".format(tx.hex(), bytes(rx).hex(),
                                                      "PASS" if ok else "FAIL"))
    return ok


def _drive_read(out_name, in_name):
    o = Pin(p(out_name), Pin.OUT, value=0)
    i = Pin(p(in_name), Pin.IN)
    ok = True
    for v in (1, 0, 1, 0):
        o.value(v)
        time.sleep_ms(5)
        if i.value() != v:
            ok = False
    Pin(p(out_name), Pin.IN)
    return ok


def test_gpio():
    a = _drive_read(LOOP_A, LOOP_B)
    b = _drive_read(LOOP_B, LOOP_A)
    print("GPIO {}->{}: {}".format(LOOP_A, LOOP_B, "PASS" if a else "FAIL"))
    print("GPIO {}->{}: {}".format(LOOP_B, LOOP_A, "PASS" if b else "FAIL"))
    return a and b


def test_adc():
    for ch, n in enumerate(("ADC0", "ADC1", "ADC2")):
        adc = ADC(Pin(p(n)))
        print("\n{} (GP{}):".format(n, p(n)))
        for i in range(10):
            raw = adc.read_u16()
            print("  {:2d}: raw={:5d}  {:.3f} V".format(i, raw, raw * VREF / 65535))
            time.sleep(0.05)


def test_square():
    sq = Pin(p(SQUARE_PIN), Pin.OUT, value=0)
    print("1 Hz square wave on {} (GP{}) for 10 s...".format(SQUARE_PIN, p(SQUARE_PIN)))
    for _ in range(10):
        sq.value(1); time.sleep(0.5)
        sq.value(0); time.sleep(0.5)
    print("Done.")


def test_all_auto():
    results = {}
    for name, fn in (("UART", test_uart), ("SPI", test_spi), ("GPIO", test_gpio)):
        try:
            results[name] = fn()
        except Exception as e:
            print("{} error: {}".format(name, e))
            results[name] = False
        safe_state()
    print("\n----- SUMMARY -----")
    for k, v in results.items():
        print("  {:5s} {}".format(k, "PASS" if v else "FAIL"))
    print("  OVERALL", "PASS" if all(results.values()) else "FAIL")


# ------------------------------------------------------------------- menu ---
MENU = [
    ("Board alive (blink LED)", test_alive),
    ("FET gate test (manual measure)", test_fets),
    ("Input monitor", test_inputs),
    ("UART loopback (jumper GP0-GP1)", test_uart),
    ("SPI loopback (jumper MOSI-MISO)", test_spi),
    ("GPIO loopback (jumper GP8-GP9)", test_gpio),
    ("ADC read", test_adc),
    ("1 Hz square wave on GP16", test_square),
    ("Run all automatic tests (4,5,6)", test_all_auto),
]


def main():
    safe_state()
    test_alive()
    while True:
        print("\n--- MENU ---")
        for i, (label, _) in enumerate(MENU, 1):
            print(" {}. {}".format(i, label))
        print(" q. Quit to REPL")
        c = input("Select: ").strip().lower()
        if c == "q":
            safe_state()
            break
        try:
            MENU[int(c) - 1][1]()
        except (ValueError, IndexError):
            print("Invalid choice.")
        except KeyboardInterrupt:
            print("\nAborted.")
        except Exception as e:
            print("Error:", e)
        finally:
            safe_state()


main()
