"""
RF power sweep: Rigol DG5352 Pro (source) -> DUT -> Rigol MSO8204 (FFT).

Steps the generator CH1 (100 MHz sine, 50 ohm) from START_DBM to STOP_DBM.
At every step the full FFT trace is captured from the scope and saved to CSV.
If the measured FFT peak reaches STOP_LIMIT_DBM, the generator output is
switched off immediately. The output is also switched off at the end of the
sweep and on any error. A Pout vs Pin graph is produced at the end.

Requirements: pip install pyvisa pyvisa-py numpy matplotlib
"""

import csv
import time
from datetime import datetime

import numpy as np
import pyvisa
import matplotlib.pyplot as plt

# ---------------------------------------------------------------- user settings
SCOPE_IP = "169.254.174.43"      # MSO8204
GEN_IP = "169.254.112.67"        # DG5352 Pro

FREQ_HZ = 100e6                # sine frequency

START_DBM = -35.0              # generator start power (dBm)
STOP_DBM = -10.0                 # generator sweep end power (dBm)
STEP_DB = 1.0                  # step size (dB)
DWELL_S = 0.5                  # time per step (s)

STOP_LIMIT_DBM = 0.0          # turn generator OFF if measured FFT peak >= this

# FFT settings
FFT_CENTER_HZ = 100e6
FFT_SPAN_HZ = 200e6
PEAK_SEARCH_HZ = 5e6           # +/- window around FREQ_HZ used to find the peak

# The MSO8204 FFT reports dBVrms. With a 50 ohm input:
# dBm = dBVrms + 10*log10(1000/50) = dBVrms + 13.01
DBV_TO_DBM = 13.0103

OUTPUT_PREFIX = "sweep_" + datetime.now().strftime("%Y%m%d_%H%M%S")
# ------------------------------------------------------------------------------


def open_instr(rm, ip):
    inst = rm.open_resource(f"TCPIP0::{ip}::INSTR")
    inst.timeout = 10000
    inst.read_termination = "\n"
    inst.write_termination = "\n"
    print(f"{ip}: {inst.query('*IDN?').strip()}")
    return inst


def check_errors(inst, name):
    """Read and clear the error queue; raise if the instrument reported errors."""
    errors = []
    for _ in range(20):
        err = inst.query(":SYSTem:ERRor?").strip()
        if err.startswith(("0", "+0")):
            break
        errors.append(err)
    if errors:
        raise RuntimeError(f"{name} reported errors: {errors}")


def configure_generator(gen):
    # Commands per the DG5000 Pro Programming Guide. The load must be set to
    # 50 ohm (:OUTPut:LOAD) before selecting DBM, as dBm is not available in HighZ.
    gen.write("*RST")
    gen.query("*OPC?")
    gen.write("*CLS")
    gen.write(":OUTPut1 OFF")
    gen.write(":OUTPut1:LOAD 50")
    gen.write(":SOURce1:FUNCtion SINusoid")
    gen.write(f":SOURce1:FREQuency {FREQ_HZ}")
    gen.write(":SOURce1:VOLTage:UNIT DBM")
    gen.write(":SOURce1:VOLTage:OFFSet 0")
    gen.query("*OPC?")
    check_errors(gen, "Generator")

    load = float(gen.query(":OUTPut1:LOAD?"))
    unit = gen.query(":SOURce1:VOLTage:UNIT?").strip().upper()
    if load != 50 or unit != "DBM":
        raise RuntimeError(f"Generator not configured: load={load} ohm, unit={unit}")
    set_gen_power(gen, START_DBM)


def set_gen_power(gen, dbm):
    # Explicit DBM suffix (Table 3.1) so the value can never be taken as Vpp.
    gen.write(f":SOURce1:VOLTage {dbm:.2f}DBM")
    readback = float(gen.query(":SOURce1:VOLTage?"))
    if abs(readback - dbm) > 0.05:
        raise RuntimeError(f"Generator amplitude reads {readback}, expected {dbm:.2f} dBm")


def gen_off(gen):
    try:
        gen.write(":OUTPut1 OFF")
        print("Generator output OFF")
    except Exception as e:
        print(f"WARNING: could not turn generator off: {e}")


def configure_scope(scope):
    scope.write("*RST")
    time.sleep(2)
    scope.write(":CHANnel1:DISPlay ON")
    scope.write(":CHANnel1:IMPedance FIFTy")
    scope.write(":CHANnel1:PROBe 1")
    scope.write(":CHANnel1:COUPling DC")
    scope.write(":CHANnel1:SCALe 0.5")          # V/div, adjust for your levels
    scope.write(":TIMebase:MAIN:SCALe 1e-6")    # enough cycles for FFT resolution
    scope.write(":TRIGger:EDGE:SOURce CHANnel1")
    scope.write(":ACQuire:TYPE NORMal")

    scope.write(":MATH1:DISPlay ON")
    scope.write(":MATH1:OPERator FFT")
    scope.write(":MATH1:FFT:SOURce CHANnel1")
    scope.write(":MATH1:FFT:WINDow HANNing")
    scope.write(":MATH1:FFT:UNIT DB")
    scope.write(f":MATH1:FFT:HCENter {FFT_CENTER_HZ}")
    scope.write(f":MATH1:FFT:HSCale {FFT_SPAN_HZ}")   # span

    scope.write(":WAVeform:SOURce MATH1")
    scope.write(":WAVeform:MODE NORMal")
    scope.write(":WAVeform:FORMat ASCii")
    scope.write(":RUN")
    scope.query("*OPC?")


def read_fft(scope):
    """Return (freq_hz, level_dbm) arrays for the current FFT trace."""
    pre = scope.query(":WAVeform:PREamble?").strip().split(",")
    x_inc, x_org, x_ref = float(pre[4]), float(pre[5]), float(pre[6])
    raw = scope.query(":WAVeform:DATA?").strip()
    if raw.startswith("#"):                     # strip IEEE block header
        n = int(raw[1])
        raw = raw[2 + n:]
    levels = np.array([float(v) for v in raw.split(",") if v.strip()])
    freqs = (np.arange(len(levels)) - x_ref) * x_inc + x_org
    return freqs, levels + DBV_TO_DBM


def peak_near(freqs, levels, f0, window):
    mask = np.abs(freqs - f0) <= window
    if not mask.any():
        return float("nan")
    return float(levels[mask].max())


def main():
    rm = pyvisa.ResourceManager("@py")          # use "" for NI/Keysight VISA
    scope = open_instr(rm, SCOPE_IP)
    gen = open_instr(rm, GEN_IP)

    configure_scope(scope)
    configure_generator(gen)

    pin_list = np.arange(START_DBM, STOP_DBM + STEP_DB / 2, STEP_DB)
    results = []                                # (pin, pout)
    traces_file = f"{OUTPUT_PREFIX}_fft_traces.csv"

    try:
        gen.write(":OUTPut1 ON")
        with open(traces_file, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["step", "pin_dbm", "freq_hz", "level_dbm"])

            for i, pin in enumerate(pin_list):
                set_gen_power(gen, pin)
                time.sleep(DWELL_S)

                freqs, levels = read_fft(scope)
                pout = peak_near(freqs, levels, FREQ_HZ, PEAK_SEARCH_HZ)
                results.append((pin, pout))
                w.writerows([i, pin, fr, lv] for fr, lv in zip(freqs, levels))
                print(f"Step {i:3d}: Pin {pin:7.2f} dBm  Pout {pout:7.2f} dBm")

                if pout >= STOP_LIMIT_DBM:
                    print(f"STOP LIMIT reached ({pout:.2f} >= {STOP_LIMIT_DBM} dBm)")
                    break
    finally:
        gen_off(gen)

    summary_file = f"{OUTPUT_PREFIX}_pout_vs_pin.csv"
    with open(summary_file, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pin_dbm", "pout_dbm"])
        w.writerows(results)

    pin, pout = zip(*results)
    plt.figure()
    plt.plot(pin, pout, "o-")
    plt.axhline(STOP_LIMIT_DBM, color="r", ls="--", label="STOP limit")
    plt.xlabel("Generator power Pin (dBm)")
    plt.ylabel(f"Measured FFT peak @ {FREQ_HZ/1e6:.0f} MHz (dBm)")
    plt.title("Pout vs Pin")
    plt.grid(True)
    plt.legend()
    plt.savefig(f"{OUTPUT_PREFIX}_pout_vs_pin.png", dpi=150)
    print(f"Saved {traces_file}, {summary_file}, {OUTPUT_PREFIX}_pout_vs_pin.png")
    plt.show()

    scope.close()
    gen.close()


if __name__ == "__main__":
    main()
