"""Independent QA oracle: manufacturer worked examples from /workspace/dam-refs.
Each case = (id, source, computed, published, abs_tol). Written by QA, not by app devs.
App reduction functions should reproduce these; run with pytest or `python reference_examples.py`."""
import json, math, os

# Cases quoted from restricted (internal-test-only) sources are NOT embedded; they are read by path.
PRIVATE = os.environ.get("DAM_QA_PRIVATE", "/workspace/dam-refs/qa_private")
_KIND = {"factor": lambda L, K: L / (2.0 * K), "asin_deg": lambda x: math.degrees(math.asin(x))}
def private_cases():
    p = os.path.join(PRIVATE, "inclinometer_expected.json")
    if not os.path.exists(p): return []
    return [(i, s, _KIND[k](*a), pub, t) for i, s, k, a, pub, t in json.load(open(p))["reference_examples"]]

def digits_from_period_us(p_us): return (1.0 / (p_us * 1e-6)) ** 2 * 1e-3
def digits_from_hz(hz): return hz ** 2 / 1000.0
def lin(G, R1, R0): return G * (R1 - R0)
def poly_C(A, B, R0): return -(A * R0**2 + B * R0)
def poly(A, B, R1, R0): return A * R1**2 + B * R1 + poly_C(A, B, R0)
def thermistor_3k(R, A=1.4051e-3, B=2.369e-4, C=1.019e-7):
    L = math.log(R); return 1.0 / (A + B * L + C * L**3) - 273.15
def kpa_to_m_h2o(p): return p * 0.10197
def inc_combined(a0, a180): return (a0 - a180) / 2.0
def inc_checksum(a0, a180): return a0 + a180
def inc_deviation(L, a0, a180, K): return L * (a0 - a180) / (2.0 * K)
def inc_displacement(L, sum_now, sum_init, K): return L * (sum_now - sum_init) / (2.0 * K)

G4420 = 0.01211
CASES = [
 # Geokon 4500 VW piezometer manual, §6.1 Eq1 (p.16 of manual)
 ("PZ-01 digits 8000 -> period 354us", "4500 §6.1", digits_from_period_us(354), 8000, 25),  # manual rounds period to 1us; +/-0.5us = +/-22.6 digits (exact period 353.55us)
 ("PZ-02 digits 8000 -> 2828 Hz", "4500 §6.1", digits_from_hz(2828), 8000, 5),
 ("PZ-03 linear P=G(R1-R0)", "4500 §6.1.1", lin(-0.1795, 6749, 9082), 418.8, 0.05),
 ("PZ-04 linear head m H2O", "4500 §6.1.1/§6.4.2", kpa_to_m_h2o(418.8), 42.7, 0.05),
 ("PZ-05 polynomial C", "4500 §6.1.2", poly_C(-7.113e-7, -0.1694, 9082), 1597.2, 0.05),
 ("PZ-06 polynomial P", "4500 §6.1.2", poly(-7.113e-7, -0.1694, 6749, 9082), 421.5, 0.05),
 ("PZ-07 poly head m H2O", "4500 §6.1.2", kpa_to_m_h2o(421.5), 43.0, 0.05),
 ("PZ-08 temp corr K(T1-T0)", "4500 §6.4.1", 0.1319 * (15 - 22), -0.92, 0.005),
 ("PZ-09 baro inHg->kPa F=3.386 (29->31 inHg)", "4500 §6.2", (31 - 29) * 3.386, 6.772, 0.001),
 ("PZ-10 baro 2 inHg ~ 1 psi", "4500 §6.2", (31 - 29) * 0.491, 1.0, 0.02),
 ("PZ-11 thermistor 3k @3000 ohm ~25C", "4500 App B.1", thermistor_3k(3000), 25.0, 0.1),
 ("PZ-12 thermistor table 9796 ohm = 0C", "4500 App B Table 8", thermistor_3k(9796), 0.0, 0.1),
 ("PZ-13 thermistor table 1598 ohm = 40C", "4500 App B Table 8", thermistor_3k(1598), 40.0, 0.1),
 ("PZ-14 thermistor table 201.1k = -50C", "4500 App B Table 8", thermistor_3k(201100), -50.0, 0.2),
 # DGSI/Slope Indicator Data Reduction & Error Correction (data_reduction.pdf p.1-3)
 ("IN-01 sin(1deg)*20000", "data_reduction p2", math.sin(math.radians(1)) * 20000, 349, 0.5),
 ("IN-02 combined (A0-A180)/2", "data_reduction p2", inc_combined(359, -339), 349, 1e-9),
 ("IN-03 checksum A0+A180 (=2x bias)", "data_reduction p2", inc_checksum(359, -339), 20, 1e-9),
 ("IN-04 deviation 24in English", "data_reduction p3", inc_deviation(24, 359, -339, 20000), 0.4188, 0.00005),
 ("IN-05 displacement 24in (700-698)", "data_reduction p3", inc_displacement(24, 700, 698, 20000), 0.0012, 0.00005),
 # Geokon 4420 VW crackmeter §6.1-6.2
 ("CK-01 linear D=(R1-R0)G", "4420 §6.1.1", lin(G4420, 6000, 4200), 21.79, 0.01),  # exact 21.798; manual truncates to 21.79
 ("CK-02 polynomial C", "4420 §6.1.2", poly_C(4.1548e-8, 0.01173, 4200), -49.999, 0.001),
 ("CK-03 polynomial D", "4420 §6.1.2", poly(4.1548e-8, 0.01173, 6000, 4200), 21.88, 0.005),
 ("CK-04 thermal K=(R1*M+B)G", "4420 §6.2.1", (6000 * 0.000376 + 0.328) * G4420, 0.031292, 1e-6),
 ("CK-05 thermal corr", "4420 §6.2.1", (6000 * 0.000376 + 0.328) * G4420 * (32.9 - 20.3), 0.394, 0.0005),
 # Geokon 4900 VW load cell §4.1-4.2 (lbs)
 ("LC-01 linear L=(R1-R0)G", "4900 §4.1", lin(-397, 5497, 7309), 719400, 50),
 ("LC-02 polynomial C", "4900 §4.1", poly_C(-0.00247, -367, 7305), 2812740, 5),
 ("LC-03 polynomial L", "4900 §4.1", poly(-0.00247, -367, 6409, 7305), 359180, 5),
 # Geokon 4000 strain gauge rosette example
 ("SG-01 avg axial strain", "4000 rosette example", (20 + 192 + 88) / 3, 100, 1e-9),
 ("SG-02 tan(theta)=80/60 -> 53 deg", "4000 rosette example", math.degrees(math.atan(80 / 60)), 53, 0.2),
 ("SG-03 principal max 60*.6+80*.8+100", "4000 rosette example", 60 * 0.6 + 80 * 0.8 + 100, 200, 1e-9),
]
_PRIV = private_cases()
if not _PRIV: print("SKIP  IN-06..IN-09 (restricted DGSI cases): %s not found" % PRIVATE)
CASES += _PRIV

def test_reference_examples():
    bad = [(i, c, p) for i, s, c, p, t in CASES if abs(c - p) > t]
    assert not bad, bad

if __name__ == "__main__":
    n = 0
    for i, s, c, p, t in CASES:
        ok = abs(c - p) <= t; n += ok
        print(f"{'PASS' if ok else 'FAIL'}  {i:45s} computed={c:.6g} published={p} tol={t}  [{s}]")
    print(f"{n}/{len(CASES)} PASS")
