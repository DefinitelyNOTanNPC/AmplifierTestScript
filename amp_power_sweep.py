#!/usr/bin/env python3
"""
amp_power_sweep.py - RF amplifier power sweep
Rigol DG5352 (generator, CH1) -> amplifier -> Rigol DHO924S (scope, CH1)

Dependencies: pyvisa, pyvisa-py (LAN, no NI-VISA needed), matplotlib.
    pip install pyvisa pyvisa-py matplotlib

Run:
    python amp_power_sweep.py --dry-run      (simulated instruments, no hardware)
    python amp_power_sweep.py --gen-ip 192.168.1.50

Generator commands: DG5000 Programming Guide (OUTPut / SOURce / SYSTem pages).
Scope commands:     DHO800/DHO900 Programming Guide (section numbers noted).
"""

import argparse
import csv
import datetime
import math
import os
import sys
import time

# ============================== CONFIG ==============================
GENERATOR_IP = None                 # DHCP: read it from the front panel, or use --gen-ip
SCOPE_IP = "169.254.112.67"

FREQ_HZ = 100e6                     # 100 MHz sine
START_DBM = -48.0                   # first generator level
STOP_DBM = -10.0                    # last generator level (upper bound)
STEP_DB = 1.0                       # 1 dB steps
MAX_GENERATOR_DBM = -10.0           # absolute ceiling for ANY amplitude command

PREDICTED_GAIN_DB = 35.0            # "figurative" gain used for the pre-check
LIMIT_SCOPE_DBM = 20.0              # measured at the scope must stay below this

# Scope CH1 for 640 mVpp .. 6.4 Vpp: 1 V/div x 8 div = 8 V span (6.4 Vpp fits,
# 640 mVpp is 0.64 div, still well resolved by the 12-bit ADC).
SCOPE_V_PER_DIV = 1.0
SCOPE_VERTICAL_DIVS = 8
SCOPE_TIMEBASE_S = 20e-9            # 20 ns/div x 10 div = 20 cycles at 100 MHz
SETTLE_S = 0.5                      # wait after each level change
ACQ_TIMEOUT_S = 5.0                 # max wait for a single acquisition
AMP_READBACK_TOL_DB = 0.05
RESULTS_DIR = "results"
# ====================================================================


class SafetyAbort(Exception):
    """Raised on any condition that must stop the test with the output OFF."""


def vrms_to_dbm(vrms):
    """Power into 50 ohm in dBm: P = Vrms^2 / 50 (W); dBm = 10*log10(P / 1 mW)."""
    return 10.0 * math.log10(vrms ** 2 / 50.0 / 0.001)


# ------------------------------ Generator ------------------------------
class Generator:
    """DG5352 CH1. The only amplitude command is sent by set_level_dbm()."""

    def __init__(self, res):
        self.res = res
        self.on = False

    def q(self, cmd):
        return self.res.query(cmd).strip()

    def off(self):
        """Output OFF (OUTPut STATe page). Never raises; returns True if confirmed."""
        try:
            self.res.write(":OUTP1 OFF")
            self.res.write(":OUTP2 OFF")
        except Exception:
            pass
        self.on = False
        try:
            return self.q(":OUTP1?").upper() == "OFF"
        except Exception:
            return False

    def fail(self, msg):
        """Output OFF first, then abort."""
        self.off()
        raise SafetyAbort(msg)

    def set_check(self, cmd, query, expect, numeric=False, tol=1e-6):
        """Send a setting, read it back, abort on mismatch."""
        self.res.write(cmd)
        got = self.q(query)
        ok = (abs(float(got) - expect) <= tol) if numeric else (got.upper() == expect)
        if not ok:
            self.fail(f"Generator read-back mismatch: {query} returned {got!r}, expected {expect!r}")

    def check_errors(self):
        """SYSTem ERRor page: '0,"No Error"' when there is no error."""
        reply = self.q(":SYST:ERR?")
        if not reply.startswith("0"):
            self.fail(f"Generator error: {reply}")

    def setup(self):
        """Configure CH1 with the output OFF: sine, 50 ohm, 1X, dBm, 100 MHz, start level."""
        if not self.off():
            raise SafetyAbort("Could not confirm generator output OFF")
        self.set_check(":SOUR1:MOD OFF", ":SOUR1:MOD?", "OFF")          # SOURce MOD STATe
        self.set_check(":SOUR1:SWE:STAT OFF", ":SOUR1:SWE:STAT?", "OFF")  # SOURce SWEep STATe
        self.set_check(":SOUR1:BURS OFF", ":SOUR1:BURS?", "OFF")        # SOURce BURSt STATe
        self.set_check(":SOUR1:FUNC SIN", ":SOUR1:FUNC?", "SIN")        # SOURce FUNCtion SHAPe
        self.set_check(":OUTP1:LOAD 50", ":OUTP1:LOAD?", 50.0, True)   # OUTPut LOAD (default High Z)
        self.set_check(":OUTP1:ATT 1X", ":OUTP1:ATT?", "1X")            # OUTPut ATTenuation
        # Unit after load: "DBM is not available when the output is High Z" (VOLTage UNIT page)
        self.set_check(":SOUR1:VOLT:UNIT DBM", ":SOUR1:VOLT:UNIT?", "DBM")
        self.set_check(":SOUR1:VOLT:OFFS 0", ":SOUR1:VOLT:OFFS?", 0.0, True, 1e-3)
        self.set_check(f":SOUR1:FREQ {FREQ_HZ:.0f}", ":SOUR1:FREQ?", FREQ_HZ, True, 1.0)
        self.set_level_dbm(START_DBM)
        self.check_errors()

    def set_level_dbm(self, dbm):
        """THE ONLY amplitude command. Refuses anything above the ceiling."""
        if not math.isfinite(dbm) or dbm > MAX_GENERATOR_DBM:
            self.fail(f"Refused amplitude {dbm} dBm (ceiling {MAX_GENERATOR_DBM} dBm)")
        self.res.write(f":SOUR1:VOLT {dbm:.2f}")   # SOURce VOLTage LEVel IMMediate AMPLitude
        got = float(self.q(":SOUR1:VOLT?"))
        if abs(got - dbm) > AMP_READBACK_TOL_DB:
            self.fail(f"Amplitude read-back {got} dBm, expected {dbm} dBm")
        self.check_errors()

    def output_on(self):
        self.res.write(":OUTP1 ON")
        if self.q(":OUTP1?").upper() != "ON":
            self.fail("Generator output did not turn ON")
        self.on = True


# ------------------------------- Scope -------------------------------
class Scope:
    """DHO924S CH1, VRMS measurement."""

    def __init__(self, res):
        self.res = res

    def q(self, cmd):
        return self.res.query(cmd).strip()

    def set_check(self, cmd, query, expect, numeric=False):
        self.res.write(cmd)
        got = self.q(query)
        ok = math.isclose(float(got), expect, abs_tol=1e-9) if numeric else got.upper() == expect
        if not ok:
            raise SafetyAbort(f"Scope read-back mismatch: {query} returned {got!r}, expected {expect!r}")

    def setup(self):
        self.res.write("*CLS")                                                   # 3.12.3
        self.set_check(":CHANnel1:DISPlay ON", ":CHANnel1:DISPlay?", "1")        # 3.6.3
        for ch in (2, 3, 4):
            self.set_check(f":CHANnel{ch}:DISPlay OFF", f":CHANnel{ch}:DISPlay?", "0")
        self.set_check(":CHANnel1:PROBe 1", ":CHANnel1:PROBe?", 1.0, True)      # 3.6.8
        self.set_check(f":CHANnel1:SCALe {SCOPE_V_PER_DIV}", ":CHANnel1:SCALe?", SCOPE_V_PER_DIV, True)  # 3.6.7
        self.set_check(":CHANnel1:OFFSet 0", ":CHANnel1:OFFSet?", 0.0, True)    # 3.6.5
        self.set_check(":CHANnel1:BWLimit OFF", ":CHANnel1:BWLimit?", "OFF")    # 3.6.1
        self.set_check(":CHANnel1:COUPling DC", ":CHANnel1:COUPling?", "DC")    # 3.6.2
        self.set_check(f":TIMebase:MAIN:SCALe {SCOPE_TIMEBASE_S}", ":TIMebase:MAIN:SCALe?", SCOPE_TIMEBASE_S, True)  # 3.26.5
        self.set_check(":ACQuire:TYPE NORMal", ":ACQuire:TYPE?", "NORM")        # 3.3.3
        self.set_check(":TRIGger:MODE EDGE", ":TRIGger:MODE?", "EDGE")          # 3.27.1
        self.set_check(":TRIGger:EDGE:SOURce CHANnel1", ":TRIGger:EDGE:SOURce?", "CHAN1")  # 3.27.8.1
        self.set_check(":TRIGger:EDGE:LEVel 0", ":TRIGger:EDGE:LEVel?", 0.0, True)        # 3.27.8.3
        for item in ("VRMS", "VMAX", "VMIN"):
            self.res.write(f":MEASure:ITEM {item},CHANnel1")                     # 3.17.2
        err = self.q(":SYSTem:ERRor?")                                          # 3.24.3
        if not err.startswith("0"):
            raise SafetyAbort(f"Scope error: {err}")

    def measure(self):
        """Fresh single acquisition, then VRMS/VMAX/VMIN. Returns (vrms, dbm).
        Raises SafetyAbort on timeout, invalid value, or clipping (a clipped sine reads LOW)."""
        self.res.write(":CLEar")                                                # 3.1.1
        self.res.write(":SINGle")                                               # 3.1.4
        self.q("*OPC?")                                                         # 3.12.6
        t0 = time.monotonic()
        while self.q(":TRIGger:STATus?").upper() != "STOP":                     # 3.27.3
            if time.monotonic() - t0 > ACQ_TIMEOUT_S:
                raise SafetyAbort("Scope acquisition timeout (no trigger)")
            time.sleep(0.05)
        try:
            vrms, vmax, vmin = (float(self.q(f":MEASure:ITEM? {i},CHANnel1")) for i in ("VRMS", "VMAX", "VMIN"))
        except ValueError as e:
            raise SafetyAbort(f"Invalid measurement reply: {e}")
        full_scale = SCOPE_V_PER_DIV * SCOPE_VERTICAL_DIVS / 2          # 4 V
        # The guide says out-of-range results are "invalid" but gives no value:
        # treat NaN/inf, non-positive or physically impossible values as invalid.
        if not all(map(math.isfinite, (vrms, vmax, vmin))) or not 0 < vrms <= full_scale:
            raise SafetyAbort(f"Invalid measurement: VRMS={vrms}")
        if vmax >= 0.95 * full_scale or vmin <= -0.95 * full_scale:
            raise SafetyAbort(f"Clipping: VMAX={vmax} V, VMIN={vmin} V")
        return vrms, vrms_to_dbm(vrms)


# ----------------------------- Dry-run sim -----------------------------
class SimBench:
    """Tiny simulator: amp gain 35 dB, compresses towards +27 dBm at the scope."""

    def __init__(self):
        self.gen = {"OUTP1": "OFF", "LOAD": "INFINITY", "UNIT": "VPP", "VOLT": 5.0}

    def scope_vrms(self):
        if self.gen["OUTP1"] != "ON":
            return None
        lin = self.gen["VOLT"] + 35.0
        dbm = lin - 10 * math.log10(1 + 10 ** ((lin - 27.0) / 10))
        return math.sqrt(50 * 1e-3 * 10 ** (dbm / 10))


class SimGen:
    def __init__(self, b):
        self.b, self.s = b, {}

    def write(self, cmd):
        head, _, arg = cmd.partition(" ")
        g = self.b.gen
        if head == ":OUTP1": g["OUTP1"] = arg
        elif head == ":OUTP1:LOAD": g["LOAD"] = "5.000000E+01"
        elif head == ":SOUR1:VOLT:UNIT": g["UNIT"] = arg
        elif head == ":SOUR1:VOLT": g["VOLT"] = float(arg)
        else: self.s[head] = arg

    def query(self, cmd):
        head = cmd[:-1]
        g = self.b.gen
        if head == ":OUTP1": return g["OUTP1"]
        if head == ":OUTP1:LOAD": return g["LOAD"]
        if head == ":SOUR1:VOLT:UNIT": return g["UNIT"]
        if head == ":SOUR1:VOLT": return f"{g['VOLT']:.6E}"
        if head == ":SYST:ERR": return '0,"No Error"'
        if head == "*IDN": return "Rigol Technologies,DG5352,SIM,00.01.07"
        return self.s.get(head, "")


class SimScope:
    MAP = {"ON": "1", "OFF": "0", "NORMAL": "NORM", "CHANNEL1": "CHAN1"}

    def __init__(self, b):
        self.b, self.s = b, {}

    def write(self, cmd):
        head, _, arg = cmd.partition(" ")
        if "DISPLAY" in head.upper():
            arg = self.MAP[arg]
        elif arg.upper() in self.MAP and "BWLIMIT" not in head.upper():
            arg = self.MAP[arg.upper()]
        self.s[head] = arg

    def query(self, cmd):
        if cmd == "*IDN?": return "RIGOL TECHNOLOGIES,DHO924S,SIM,00.01.00"
        if cmd == "*OPC?": return "1"
        if cmd == ":SYSTem:ERRor?": return '0,"No error"'
        if cmd == ":TRIGger:STATus?": return "STOP" if self.b.scope_vrms() else "WAIT"
        if cmd.startswith(":MEASure:ITEM?"):
            v = self.b.scope_vrms()
            val = {"VRMS": v, "VMAX": v * 1.414, "VMIN": -v * 1.414}[cmd.split()[1].split(",")[0]]
            return f"{val:.6E}"
        return self.s.get(cmd[:-1], "")

    def close(self): pass


# ------------------------------ Main flow ------------------------------
def open_instruments(args):
    if args.dry_run:
        b = SimBench()
        return SimGen(b), SimScope(b)
    import pyvisa
    ip = args.gen_ip or GENERATOR_IP
    if not ip:
        sys.exit("Set GENERATOR_IP in CONFIG or pass --gen-ip")
    rm = pyvisa.ResourceManager("@py")
    gen = rm.open_resource(f"TCPIP::{ip}::INSTR", timeout=5000,
                           read_termination="\n", write_termination="\n")
    scope = rm.open_resource(f"TCPIP::{SCOPE_IP}::INSTR", timeout=5000,
                             read_termination="\n", write_termination="\n")
    return gen, scope


def plot(rows, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    x = [r[0] for r in rows]
    y = [r[2] for r in rows]
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(x, y, "o-", label="Measured at scope")
    ax.axhline(LIMIT_SCOPE_DBM, color="r", ls="--", label=f"Limit {LIMIT_SCOPE_DBM} dBm")
    ax.set_xlabel("Generator output / amplifier input (dBm)")
    ax.set_ylabel("Power at scope (dBm)")
    ax.set_title(f"Input vs output power at {FREQ_HZ / 1e6:.0f} MHz")
    ax.grid(True)
    ax.legend()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="use simulated instruments")
    ap.add_argument("--gen-ip", help="generator IP (overrides CONFIG)")
    args = ap.parse_args()

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(RESULTS_DIR, exist_ok=True)
    base = os.path.join(RESULTS_DIR, f"sweep_{stamp}{'_DRYRUN' if args.dry_run else ''}")
    rows = []
    gen = None
    try:
        gen_res, scope_res = open_instruments(args)
        gen = Generator(gen_res)
        if not gen.off():                               # output OFF at script start
            raise SafetyAbort("Could not confirm generator output OFF at start")
        gen_idn, scope_idn = gen.q("*IDN?"), scope_res.query("*IDN?").strip()
        if "DG5352" not in gen_idn or "DHO924S" not in scope_idn:
            raise SafetyAbort(f"Unexpected instruments: {gen_idn} / {scope_idn}")
        print(f"Generator: {gen_idn}\nScope:     {scope_idn}")

        scope = Scope(scope_res)
        scope.setup()
        gen.setup()

        level = START_DBM
        # Pre-check the first level too: predicted scope power must be below the limit.
        if level + PREDICTED_GAIN_DB >= LIMIT_SCOPE_DBM:
            raise SafetyAbort("Start level fails the +35 dB pre-check")
        gen.output_on()
        while True:
            time.sleep(0 if args.dry_run else SETTLE_S)
            vrms, dbm = scope.measure()
            # SAFETY: measured value must stay below the limit, else OFF first, then report.
            if not dbm < LIMIT_SCOPE_DBM:
                gen.fail(f"Measured {dbm:.2f} dBm at scope >= {LIMIT_SCOPE_DBM} dBm")
            rows.append((level, vrms, dbm))
            print(f"gen {level:6.1f} dBm | Vrms {vrms:.4f} V | scope {dbm:6.2f} dBm")

            nxt = level + STEP_DB
            if nxt > STOP_DBM + 1e-9:
                break
            # SAFETY pre-check: next level + 35 dB must stay below +20 dBm at the scope.
            if nxt + PREDICTED_GAIN_DB >= LIMIT_SCOPE_DBM:
                print(f"Stopping: next level {nxt} dBm + {PREDICTED_GAIN_DB} dB would reach "
                      f"{nxt + PREDICTED_GAIN_DB} dBm (limit {LIMIT_SCOPE_DBM})")
                break
            gen.set_level_dbm(nxt)
            level = nxt
        result = 0
    except (SafetyAbort, KeyboardInterrupt, Exception) as e:
        if gen:
            gen.off()                                   # OFF first
        print(f"ABORTED: {type(e).__name__}: {e}")
        result = 2
    finally:
        if gen and not gen.off():
            print("CRITICAL: generator output state UNKNOWN - press Output / switch it off now!")
            result = 3

    if rows:
        with open(base + ".csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["# timestamp", stamp, "freq_hz", FREQ_HZ, "dry_run", args.dry_run])
            w.writerow(["gen_dbm", "vrms_v", "scope_dbm"])
            w.writerows(rows)
        plot(rows, base + ".png")
        print(f"Saved {base}.csv and {base}.png")
    return result


if __name__ == "__main__":
    sys.exit(main())
