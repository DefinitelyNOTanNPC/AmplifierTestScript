"""
RF power sweep: Rigol DG5352 Pro (source) -> DUT -> Rigol MSO8204 (Vrms).

Steps the generator CH1 (100 MHz sine, 50 ohm, dBm) from START_DBM to STOP_DBM.
At every step the scope measures Vrms on CH1 (50 ohm input) and converts it to
power: dBm = 10*log10(Vrms^2 / 50 / 1e-3). The CH1 vertical scale is adjusted
automatically so the signal fills the screen without clipping. The raw CH1
waveform is read from memory at every step, and after the sweep an FFT of each
waveform is calculated on the PC, saved to CSV and plotted.

If the measured power reaches STOP_LIMIT_DBM, the generator output is switched
off immediately. The output is also switched off at the end of the sweep and on
any error. A Pout vs Pin graph is produced at the end.

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

STOP_LIMIT_DBM = 0.0          # turn generator OFF if measured power >= this

LOAD_OHMS = 50.0               # scope input impedance used for dBm conversion

# Auto vertical scale: keep the sine peak between these many divisions from
# centre (the screen is +/-4 div). Range at 50 ohm, 1x probe: 1 mV to 1 V/div.
TARGET_PEAK_DIV = 3.0
MIN_PEAK_DIV = 1.5
MAX_PEAK_DIV = 3.8
SCALE_STEPS = [1e-3, 2e-3, 5e-3, 10e-3, 20e-3, 50e-3, 100e-3, 200e-3, 500e-3, 1.0]
SETTLE_S = 0.3                 # wait after a scale change before measuring

# Waveform capture: 10k points over 10 us (1 us/div) = 1 GSa/s, so the FFT
# covers DC to 500 MHz with 100 kHz resolution.
TIMEBASE_S_PER_DIV = 1e-6
MEMORY_DEPTH = 10000

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
    # *RST can take several seconds; wait for it to finish so the following
    # commands are not dropped.
    scope.timeout = 30000
    scope.write("*RST")
    scope.query("*OPC?")
    scope.timeout = 10000
    scope.write("*CLS")
    scope.write(":CHANnel1:DISPlay ON")
    scope.write(":CHANnel1:IMPedance FIFTy")
    scope.write(":CHANnel1:PROBe 1")
    scope.write(":CHANnel1:COUPling DC")
    scope.write(":CHANnel1:OFFSet 0")
    scope.write(f":CHANnel1:SCALe {scale_for_dbm(START_DBM)}")
    scope.write(f":TIMebase:MAIN:SCALe {TIMEBASE_S_PER_DIV}")
    scope.write(f":ACQuire:MDEPth {MEMORY_DEPTH}")
    scope.write(":TRIGger:EDGE:SOURce CHANnel1")
    scope.write(":TRIGger:EDGE:LEVel 0")
    scope.write(":ACQuire:TYPE NORMal")

    scope.write(":WAVeform:SOURce CHANnel1")
    scope.write(":WAVeform:MODE RAW")
    scope.write(":WAVeform:FORMat BYTE")
    scope.write(":RUN")
    scope.query("*OPC?")
    check_errors(scope, "Scope")

    checks = {
        ":CHANnel1:IMPedance?": "FIFT",
        ":CHANnel1:PROBe?": "1",
        ":WAVeform:SOURce?": "CHAN1",
    }
    for cmd, expected in checks.items():
        got = scope.query(cmd).strip().upper()
        if not got.startswith(expected):
            raise RuntimeError(f"Scope {cmd} returned {got}, expected {expected}")
    mdep = float(scope.query(":ACQuire:MDEPth?"))
    if mdep != MEMORY_DEPTH:
        raise RuntimeError(f"Scope memory depth is {mdep:g}, expected {MEMORY_DEPTH}")


def vrms_to_dbm(vrms):
    return 10 * np.log10(vrms ** 2 / LOAD_OHMS / 1e-3)


def dbm_to_vrms(dbm):
    return np.sqrt(LOAD_OHMS * 1e-3 * 10 ** (dbm / 10))


def scale_for_vpk(vpk):
    """Smallest V/div that keeps the peak at or below TARGET_PEAK_DIV."""
    for sc in SCALE_STEPS:
        if vpk / sc <= TARGET_PEAK_DIV:
            return sc
    return SCALE_STEPS[-1]


def scale_for_dbm(dbm):
    return scale_for_vpk(dbm_to_vrms(dbm) * np.sqrt(2))


def measure_vrms(scope):
    """Measure CH1 Vrms, re-ranging the vertical scale until the peak is on screen.

    Returns (vrms, scale). Raises if no valid reading can be made.
    """
    for _ in range(len(SCALE_STEPS)):
        scale = float(scope.query(":CHANnel1:SCALe?"))
        vrms = float(scope.query(":MEASure:ITEM? VRMS,CHANnel1"))
        vmax = float(scope.query(":MEASure:ITEM? VMAX,CHANnel1"))
        vmin = float(scope.query(":MEASure:ITEM? VMIN,CHANnel1"))
        invalid = any(abs(v) > 1e30 for v in (vrms, vmax, vmin))   # 9.9E37 = no reading
        peak_div = max(abs(vmax), abs(vmin)) / scale if not invalid else float("inf")

        if not invalid and MIN_PEAK_DIV <= peak_div <= MAX_PEAK_DIV:
            return vrms, scale
        if invalid or peak_div > MAX_PEAK_DIV:          # clipped: zoom out
            bigger = [sc for sc in SCALE_STEPS if sc > scale * 1.01]
            if not bigger:
                raise RuntimeError(f"Signal exceeds 1 V/div range (Vmax {vmax}, Vmin {vmin})")
            new = bigger[0]
        else:                                           # too small: zoom in
            new = scale_for_vpk(max(abs(vmax), abs(vmin)))
            if new >= scale:
                return vrms, scale                      # already at the finest useful scale
        scope.write(f":CHANnel1:SCALe {new}")
        time.sleep(SETTLE_S)
    raise RuntimeError("Could not find a vertical scale with a valid Vrms reading")


def read_block(inst, cmd):
    """Send a query and return the payload of its TMC block (#N<len><data>) as bytes.

    Reads until the instrument's END indicator instead of trusting the length
    in the header, so a short or late block raises a clear error, not a timeout.
    """
    inst.write(cmd)
    term = inst.read_termination
    inst.read_termination = None                # binary data may contain 0x0A
    try:
        raw = inst.read_raw()
        start = raw.find(b"#")
        if start >= 0 and len(raw) >= start + 2:
            ndig = int(raw[start + 1:start + 2])
            need = start + 2 + ndig + int(raw[start + 2:start + 2 + ndig])
            while len(raw) < need:              # block arrived in several transfers
                raw += inst.read_raw()
    finally:
        inst.read_termination = term
    start = raw.find(b"#")
    if start < 0:
        raise RuntimeError(f"No data block in reply to {cmd}: {raw[:40]!r}")
    ndig = int(raw[start + 1:start + 2])
    length = int(raw[start + 2:start + 2 + ndig])
    payload = raw[start + 2 + ndig:start + 2 + ndig + length]
    if len(payload) != length:
        raise RuntimeError(f"{cmd} block header says {length} bytes, received {len(payload)}")
    return np.frombuffer(payload, dtype=np.uint8)


def read_waveform(scope):
    """Read the full CH1 record from memory. Returns (time_s, volts).

    RAW mode needs the scope stopped; it is restarted afterwards. Conversion
    per the guide: volts = (byte - YORigin - YREFerence) * YINCrement.
    """
    scope.write(":STOP")
    scope.query("*OPC?")                        # wait until acquisition has stopped
    try:
        scope.write(":WAVeform:STARt 1")
        scope.write(f":WAVeform:STOP {MEMORY_DEPTH}")
        pre = scope.query(":WAVeform:PREamble?").strip().split(",")
        x_inc, x_org, x_ref = float(pre[4]), float(pre[5]), float(pre[6])
        y_inc, y_org, y_ref = float(pre[7]), float(pre[8]), float(pre[9])
        data = read_block(scope, ":WAVeform:DATA?")
        check_errors(scope, "Scope (waveform read)")
    finally:
        scope.write(":RUN")
    volts = (data.astype(float) - y_org - y_ref) * y_inc
    t = (np.arange(volts.size) - x_ref) * x_inc + x_org
    return t, volts


def read_waveform_retry(scope, attempts=3):
    """read_waveform with retries. Returns None if every attempt fails, so a
    failed capture skips the waveform/FFT for that step instead of ending the sweep."""
    for n in range(1, attempts + 1):
        try:
            return read_waveform(scope)
        except (pyvisa.errors.VisaIOError, RuntimeError) as e:
            print(f"  waveform read failed (attempt {n}/{attempts}): {e}")
            try:
                scope.clear()                   # flush any partial reply
                scope.write(":RUN")
                time.sleep(SETTLE_S)
            except pyvisa.errors.VisaIOError:
                pass
    print("  WARNING: no waveform saved for this step")
    return None


def spectrum_dbm(t, volts):
    """Single-sided Hann-windowed spectrum in dBm (sine power into LOAD_OHMS)."""
    n = volts.size
    win = np.hanning(n)
    amp = 2 * np.abs(np.fft.rfft((volts - volts.mean()) * win)) / win.sum()  # peak V
    freqs = np.fft.rfftfreq(n, d=t[1] - t[0])
    vrms = np.maximum(amp / np.sqrt(2), 1e-12)
    return freqs, vrms_to_dbm(vrms)


def main():
    rm = pyvisa.ResourceManager("@py")          # use "" for NI/Keysight VISA
    scope = open_instr(rm, SCOPE_IP)
    gen = open_instr(rm, GEN_IP)

    configure_scope(scope)
    configure_generator(gen)
    pin_list = np.arange(START_DBM, STOP_DBM + STEP_DB / 2, STEP_DB)
    results = []                                # (pin, vrms, pout, scale)
    waveforms = []                              # (step, pin, t, volts)
    traces_file = f"{OUTPUT_PREFIX}_ch1_waveforms.csv"

    try:
        gen.write(":OUTPut1 ON")
        with open(traces_file, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["step", "pin_dbm", "time_s", "volts"])

            for i, pin in enumerate(pin_list):
                set_gen_power(gen, pin)
                time.sleep(DWELL_S)

                vrms, scale = measure_vrms(scope)
                pout = float(vrms_to_dbm(vrms))
                results.append((pin, vrms, pout, scale))
                if pout >= STOP_LIMIT_DBM:
                    gen_off(gen)                # act on the limit before anything else

                wf = read_waveform_retry(scope)
                if wf is not None:
                    t, volts = wf
                    waveforms.append((i, pin, t, volts))
                    w.writerows([i, pin, tt, v] for tt, v in zip(t, volts))
                print(f"Step {i:3d}: Pin {pin:7.2f} dBm  Vrms {vrms*1e3:8.3f} mV  "
                      f"Pout {pout:7.2f} dBm  ({scale*1e3:g} mV/div)")

                if pout >= STOP_LIMIT_DBM:
                    print(f"STOP LIMIT reached ({pout:.2f} >= {STOP_LIMIT_DBM} dBm)")
                    break
    finally:
        gen_off(gen)

    summary_file = f"{OUTPUT_PREFIX}_pout_vs_pin.csv"
    with open(summary_file, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pin_dbm", "vrms_v", "pout_dbm", "scale_v_per_div"])
        w.writerows(results)

    pin, _, pout, _ = zip(*results)
    plt.figure()
    plt.plot(pin, pout, "o-")
    plt.axhline(STOP_LIMIT_DBM, color="r", ls="--", label="STOP limit")
    plt.xlabel("Generator power Pin (dBm)")
    plt.ylabel(f"Measured power into {LOAD_OHMS:g} ohm (dBm)")
    plt.title("Pout vs Pin")
    plt.grid(True)
    plt.legend()
    plt.savefig(f"{OUTPUT_PREFIX}_pout_vs_pin.png", dpi=150)
    print(f"Saved {traces_file}, {summary_file}, {OUTPUT_PREFIX}_pout_vs_pin.png")
    plt.show()

    # FFT of every captured waveform
    spectra_file = f"{OUTPUT_PREFIX}_fft_spectra.csv"
    plt.figure(figsize=(10, 6))
    cmap = plt.get_cmap("viridis")
    with open(spectra_file, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["step", "pin_dbm", "freq_hz", "level_dbm"])
        for k, (i, p_in, t, volts) in enumerate(waveforms):
            freqs, level = spectrum_dbm(t, volts)
            w.writerows([i, p_in, fr, lv] for fr, lv in zip(freqs, level))
            plt.plot(freqs / 1e6, level, lw=0.8,
                     color=cmap(k / max(len(waveforms) - 1, 1)),
                     label=f"{p_in:.0f} dBm")
    plt.xlabel("Frequency (MHz)")
    plt.ylabel(f"Level into {LOAD_OHMS:g} ohm (dBm)")
    plt.title("CH1 spectrum at each step (Pin shown in legend)")
    plt.grid(True)
    plt.legend(fontsize=6, ncol=2, loc="upper right")
    plt.savefig(f"{OUTPUT_PREFIX}_fft_spectra.png", dpi=150)
    print(f"Saved {spectra_file}, {OUTPUT_PREFIX}_fft_spectra.png")
    plt.show()

    scope.close()
    gen.close()


if __name__ == "__main__":
    main()
