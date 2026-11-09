"""QA independent check of piezometer procedure worked examples WE1-WE6 (QA-004 follow-up).

Everything is read BY PATH; no reference data is embedded here:
  * inputs + the procedure's stated results are parsed from /workspace/dam-refs/procedures/piezometers.md §6;
  * every cited input and published result is confirmed on the cited PDF page (pdftotext -layout);
  * results are recomputed with QA's own formulas (only physical constants are in code: g, psi->kPa, ln);
  * WE6 fits HST on datasets/piezometers/synthetic/syn_pz_hst.csv (SYNTHETIC) and reads the generator's
    true coefficients from piezometers/verify.py by path (so WE6 is self-consistency, not external verification).
Skips (exit 0) if /workspace/dam-refs is absent. Classes: SOURCE (published value in PDF), ILLUS (illustrative
inputs, arithmetic only), SYNTH (synthetic data)."""
import csv, math, os, re, subprocess, sys
R = os.environ.get('DAM_REFS', '/workspace/dam-refs'); PZ = os.path.join(R, 'piezometers')
PROC = os.path.join(R, 'procedures', 'piezometers.md')
if not os.path.exists(PROC): print('SKIP: %s not found' % PROC); sys.exit(0)
G0, PSI_KPA = 9.80665, 6.894757
PRIV = os.path.join(os.environ.get('DAM_QA_PRIVATE', os.path.join(R, 'qa_private')), 'piezometer_expected.json')
if not os.path.exists(PRIV): print('SKIP: %s not found (published values are read by path only)' % PRIV); sys.exit(0)
import json; E = json.load(open(PRIV))
res = []
def chk(cls, name, ok, det=''):
    res.append((cls, bool(ok))); print('%s  [%s] %s%s' % ('PASS' if ok else 'FAIL', cls, name, ('  -- ' + str(det)) if det else ''))
_pc = {}
def pdf(f, p):
    k = (f, p)
    if k not in _pc:
        _pc[k] = re.sub(r'[ \t]+', ' ', subprocess.run(['pdftotext', '-layout', '-f', str(p), '-l', str(p), os.path.join(PZ, f), '-'],
                                                       capture_output=True, text=True).stdout).replace('–', '-').replace('−', '-')
    return _pc[k]
def on(f, p, *toks):
    t = pdf(f, p); miss = [s for s in toks if s not in t]; return not miss, miss
md = open(PROC, encoding='utf8').read().replace('−', '-').replace('–', '-')
def we(n):
    m = re.search(r'\*\*WE%d\.(.*?)(?=\*\*WE%d\.|\n## )' % (n, n + 1), md, re.S); return m.group(1) if m else ''
def num(pat, s, i=1):
    m = re.search(pat, s, re.S)
    if not m: raise SystemExit('procedure text changed, pattern not found: ' + pat)
    return m.group(i)
close = lambda a, b, t: abs(a - b) <= t

# ---- WE1 Geokon 4500 VW ----
w = we(1)
R0, R1, G = num(r'R0 = (\d+)', w), num(r'R1 = (\d+)', w), num(r'G = (-[\d.]+)', w)
K, T0, T1 = num(r'K = \+([\d.]+)', w), num(r'T0 = (\d+)', w), num(r'T1 = (\d+)', w)
S0, S1, tip = float(num(r'S0 = ([\d.]+)', w)), float(num(r'S1 = ([\d.]+)', w)), float(num(r'tip at ([\d.]+) m', w))
A, B, C = num(r'A = (-[\d.e-]+)', w), num(r'B = (-[\d.]+)', w), num(r'C = ([\d.]+) ->', w.replace('→', '->'))
Cm, Rz, Pm = num(r'C = ([\d.]+) from the calibration zero (\d+) gives \*\*([\d.]+)', w), num(r'calibration zero (\d+)', w), num(r'gives \*\*([\d.]+) kPa', w)
ok, miss = on('4500_Piezometer.pdf', 22, R0, R1, G.lstrip('-'), str(E['p_lin'])); chk('SOURCE', 'WE1 inputs R0/R1/G and published P on 4500 PDF p22', ok, miss)
P = float(G) * (float(R1) - float(R0))
chk('SOURCE', 'WE1 P = G(R1-R0) = %.3f kPa vs published %s (manual rounding)' % (P, E['p_lin']), close(P, E['p_lin'], 0.05))
chk('ILLUS', 'WE1 procedure-stated P matches recompute', close(P, float(num(r'= \*\*([\d.]+) kPa\*\* \(manual rounds', w)), 0.005))
ok, miss = on('4500_Piezometer.pdf', 25, K, E['t_tok'], str(E['t_corr'])); chk('SOURCE', 'WE1 K, T1-T0 and published correction on 4500 PDF p25', ok, miss)
Tc = float(K) * (float(T1) - float(T0)); chk('SOURCE', 'WE1 temp corr K(T1-T0) = %.4f vs published %s' % (Tc, E['t_corr']), close(Tc, E['t_corr'], 0.005))
Pc = P + Tc - (S1 - S0)
chk('ILLUS', 'WE1 Pcorr (illustrative baro) = %.3f vs procedure' % Pc, close(Pc, float(num(r'Pcorr = \*\*([\d.]+)', w)), 0.005))
h = Pc / G0; chk('ILLUS', 'WE1 head = %.4f m vs procedure; EL = %.3f vs procedure' % (h, tip + h),
                 close(h, float(num(r'h = [\d.]+/[\d.]+ = \*\*([\d.]+)', w)), 0.0005) and close(tip + h, float(num(r'EL = \*\*([\d.]+)', w)), 0.0005))
ok, miss = on('4500_Piezometer.pdf', 23, A.lstrip('-').split('e')[0], B.lstrip('-'), C, str(E['p_poly'])); chk('SOURCE', 'WE1 poly A/B/C and published P on 4500 PDF p23', ok, miss)
Pp = float(A) * float(R1) ** 2 + float(B) * float(R1) + float(C); chk('SOURCE', 'WE1 poly P = %.2f vs published %s' % (Pp, E['p_poly']), close(Pp, E['p_poly'], 0.05))
Cc = -(float(A) * float(R0) ** 2 + float(B) * float(R0)); chk('SOURCE', 'WE1 poly C = -(A R0^2 + B R0) = %.2f vs published %s' % (Cc, C), close(Cc, float(C), 0.05))
ok, miss = on('Geokon_4500MLP_Multilevel.pdf', 35, Cm, Rz, Pm, E['mlp_true']); chk('SOURCE', 'WE1 4500MLP C/zero/P on MLP PDF p35', ok, miss)
Cz = -(float(A) * float(Rz) ** 2 + float(B) * float(Rz)); Pz = float(A) * float(R1) ** 2 + float(B) * float(R1) + float(Cm)
chk('SOURCE', 'WE1 MLP C from zero = %.2f (pub %s); P = %.2f (pub %s)' % (Cz, Cm, Pz, Pm), close(Cz, float(Cm), 0.05) and close(Pz, float(Pm), 0.005))

# ---- WE2 Roctest FOP ----
w = we(2)
Lc = num(r'Lc = (\d+)', w); c = [num(r'c%d = (-?[\d.E+-]+)' % i, w).rstrip(';') for i in range(4)]
L0, T0, B0 = num(r'L0 = (\d+)', w), num(r'T0 = (\d+)', w), num(r'B0 = ([\d.]+)', w)
L1, T1, B1, CT = num(r'L1 = (\d+)', w), num(r'T1 = (\d+)', w), num(r'B1 = ([\d.]+)', w), num(r'CT = (-[\d.]+)', w)
ok, miss = on('Roctest_FOP_Piezometer.pdf', 13, Lc, c[0], c[1].lstrip('-')); chk('SOURCE', 'WE2 Lc, c0, c1 on Roctest PDF p13', ok, miss)
ok, miss = on('Roctest_FOP_Piezometer.pdf', 14, c[2], c[3].lstrip('-'), L0, L1, str(E['rt_p0_psi']), str(E['rt_p1_psi'])); chk('SOURCE', 'WE2 c2, c3, L0, L1, published Prec0/Prec1 psi on Roctest PDF p14', ok, miss)
ok, miss = on('Roctest_FOP_Piezometer.pdf', 15, CT.lstrip('-'), B0, B1, '(%s - %s)' % (T1, T0), str(E['rt_p0_kpa']), str(E['rt_p1_kpa']), str(E['rt_pcorr'])); chk('SOURCE', 'WE2 CT, B0, B1, T1-T0 and published kPa values on Roctest PDF p15', ok, miss)
cf = [float(x) for x in c]
poly = lambda L: sum(cf[i] * (L - float(Lc)) ** i for i in range(4))
p0, p1 = poly(float(L0)), poly(float(L1))
chk('SOURCE', 'WE2 Prec0 = %.4f psi (pub %s), Prec1 = %.4f psi (pub %s)' % (p0, E['rt_p0_psi'], p1, E['rt_p1_psi']), close(p0, E['rt_p0_psi'], 0.005) and close(p1, E['rt_p1_psi'], 0.005))
chk('SOURCE', 'WE2 kPa from rounded psi: %.2f / %.2f (pub %s / %s)' % (E['rt_p0_psi'] * PSI_KPA, E['rt_p1_psi'] * PSI_KPA, E['rt_p0_kpa'], E['rt_p1_kpa']),
    close(E['rt_p0_psi'] * PSI_KPA, E['rt_p0_kpa'], 0.005) and close(E['rt_p1_psi'] * PSI_KPA, E['rt_p1_kpa'], 0.005))
pc = (E['rt_p1_kpa'] - E['rt_p0_kpa']) - float(CT) * (float(T1) - float(T0)) - (float(B1) - float(B0))
chk('SOURCE', 'WE2 Pcorr = (P1-P0) - CT(T1-T0) - (B1-B0) = %.3f (pub %s)' % (pc, E['rt_pcorr']), close(pc, E['rt_pcorr'], 0.0005))
pcu = (p1 - p0) * PSI_KPA - float(CT) * (float(T1) - float(T0)) - (float(B1) - float(B0))
chk('SOURCE', 'WE2 Pcorr from unrounded Prec = %.3f kPa (rounding sensitivity <= 0.05)' % pcu, close(pcu, E['rt_pcorr'], 0.05))

# ---- WE3 standpipe (illustrative numbers; USBR PDF p86-87 rule) ----
w = we(3)
top, dip, tip3 = float(num(r'Top of pipe ([\d.]+) m', w)), float(num(r'dip ([\d.]+) m', w)), float(num(r'Tip at ([\d.]+) m', w))
el = top - dip; u = (el - tip3) * G0
chk('ILLUS', 'WE3 EL = top - dip = %.3f (proc %s); u = %.2f kPa (proc %s)' % (el, num(r'water EL \*\*([\d.]+)', w), u, num(r'= \*\*([\d.]+) kPa', w)),
    close(el, float(num(r'water EL \*\*([\d.]+)', w)), 5e-4) and close(u, float(num(r'= \*\*([\d.]+) kPa', w)), 0.005))
t86 = pdf('USBR_Embankment_Dam_Instrumentation_Manual_1987.pdf', 86) + pdf('USBR_Embankment_Dam_Instrumentation_Manual_1987.pdf', 87)
chk('SOURCE', 'WE3 USBR PDF p86-87 contain standpipe/observation-well water-level text', re.search(r'(?i)standpipe|observation well', t86) is not None)

# ---- WE4 CS451 / pneumatic ----
w = we(4)
a1, f1, r1 = num(r'([\d.]+) psig × ([\d.]+) = \*\*([\d.]+) ft', w, 1), num(r'([\d.]+) psig × ([\d.]+) = \*\*([\d.]+) ft', w, 2), num(r'([\d.]+) psig × ([\d.]+) = \*\*([\d.]+) ft', w, 3)
ok, miss = on('Campbell_CS451.pdf', 15, a1, f1, r1); chk('SOURCE', 'WE4 CS451 %s psig x %s = %s ft on CS451 PDF p15' % (a1, f1, r1), ok, miss)
chk('SOURCE', 'WE4 %s x %s = %.4f (pub %s)' % (a1, f1, float(a1) * float(f1), r1), close(float(a1) * float(f1), float(r1), 0.0006))
a2, b2, o2 = num(r'([\d.]+) psig? × [\d.]+ = ([\d.]+) ft', w, 1), num(r'([\d.]+) psig? × [\d.]+ = ([\d.]+) ft', w, 2), num(r'offset = \d+ - [\d.]+ = \*\*([\d.]+)', w)
ok19, m19 = on('Campbell_CS451.pdf', 19, a2, b2); ok20, m20 = on('Campbell_CS451.pdf', 20, b2, o2)
chk('SOURCE', 'WE4 CS451 %s psi -> %s ft on PDF p19, offset %s ft on PDF p20' % (a2, b2, o2), ok19 and ok20, m19 + m20)
chk('SOURCE', 'WE4 offset recompute = %.4f (pub %s)' % (E['cs451_ref'] - float(a2) * float(f1), o2), close(E['cs451_ref'] - float(a2) * float(f1), float(o2), 1e-6) and E['cs451_ref_ft'] in pdf('Campbell_CS451.pdf', 20))
chk('SOURCE', 'WE4 procedure cites the %s psig->ft step to CS451 PDF p19, where input and product are (QA-012 citation fix)' % a2, ok19 and re.search(re.escape(a2) + r' psig? × [\d.]+ = [\d.]+ ft \[CAMPBELL-CS451 PDF p19', w) is not None)
pn = float(num(r'Pneumatic ([\d.]+) psi', w)); ok, miss = on('USBR_Embankment_Dam_Instrumentation_Manual_1987.pdf', 62, f1)
chk('SOURCE', 'WE4 2.31 ft/psi factor on USBR PDF p62', ok, miss)
chk('ILLUS', 'WE4 pneumatic %.1f psi x 2.31 = %.2f ft (proc %s)' % (pn, pn * 2.31, num(r'= \*\*([\d.]+) ft\*\*, plus', w)), close(pn * 2.31, float(num(r'= \*\*([\d.]+) ft\*\*, plus', w)), 1e-9))

# ---- WE5 Hvorslev ----
w = we(5)
d, L, D = float(num(r'd = ([\d.]+) cm', w)), float(num(r'L = ([\d.]+) cm', w)), float(num(r'D = ([\d.]+) cm', w))
kh, kv = float(num(r'kh = ([\d.]+)e-9', w)) * 1e-9, float(num(r'kv = ([\d.]+)e-9', w)) * 1e-9
ok, miss = on('Hvorslev_1951_WES_Bulletin36_TimeLag.pdf', 46, *E['hv_toks'])
chk('SOURCE', 'WE5 inputs and published 2mL/D, T, measured T on Hvorslev PDF p46 (kh "1175" is OCR-garbled on one line)', ok, miss)
m = math.sqrt(kh / kv); r = 2 * m * L / D; T = d ** 2 * math.log(r) / (8 * L * kh)
chk('SOURCE', 'WE5 m = %.3f, 2mL/D = %.1f (pub %s), T = %.0f s (pub %s; manual rounding)' % (m, r, E['hv_r'], T, E['hv_T']), close(r, E['hv_r'], 0.5) and close(T, E['hv_T'], 15))
t272 = subprocess.run(['pdftotext', '-layout', '-f', '272', '-l', '272', os.path.join(R, 'inclinometers', 'USACE_EM_1110-2-1908_2020.pdf'), '-'], capture_output=True, text=True).stdout
chk('SOURCE', 'WE5 USACE Eq C.2 constant 3.3x10^-6 on PDF p272 == ln10/(8*86400) = %.3e' % (math.log(10) / (8 * 86400)),
    '3.3x10' in t272 and 'Equation C.2' in t272 and close(math.log(10) / (8 * 86400), 3.3e-6, 0.05e-6))
chk('SOURCE', 'WE5 t90 = ln10*T = 2.30T (Hvorslev "2.3")', close(math.log(10), 2.3, 0.01))

# ---- WE6 HST on SYNTHETIC csv ----
try:
    import numpy as np
except ImportError:
    np = None
SYN = os.path.join(R, 'datasets', 'piezometers', 'synthetic', 'syn_pz_hst.csv')
if np is None or not os.path.exists(SYN):
    print('SKIP  WE6 (numpy or %s missing)' % SYN)
else:
    rows = list(csv.DictReader(open(SYN)))
    chk('SYNTH', 'WE6 csv flagged is_synthetic=true on every row (%d rows)' % len(rows), all(x['is_synthetic'] == 'true' for x in rows))
    # True coefficients come from the GENERATOR's model expression (gen_syn_pz_hst.py), not from verify.py's
    # expected values, so this stays independent of how verify.py states them (20:5x EST: verify.py dropped true=dict).
    gsrc = open(os.path.join(PZ, 'gen_syn_pz_hst.py')).read()
    yexpr = re.search(r'^y = \((.*?)\)\s*$', gsrc, re.S | re.M).group(1)
    true = {'a2': float(re.search(r'([+-]\s*[\d.]+)\*t\b', yexpr).group(1).replace(' ', '')),
            'a7': float(re.search(r'([+-]\s*[\d.]+)\*np\.cos\(S\)', yexpr).group(1).replace(' ', ''))}
    zmax, zmin = (float(x) for x in re.search(r'zmax, zmin = ([\d.]+), ([\d.]+)', gsrc).groups())
    dd = np.array([float(x['day']) for x in rows]); H = np.array([float(x['reservoir_m']) for x in rows]); y = np.array([float(x['piezo_level_m']) for x in rows])
    t = dd / 365.25; S = 2 * np.pi * (dd % 365.25) / 365.25; z = (zmax - H) / (zmax - zmin)
    X = np.column_stack([np.ones_like(t), np.exp(-t / 2.0), t, z, z ** 2, z ** 3, z ** 4, np.cos(S), np.sin(S), np.sin(2 * S), np.cos(2 * S)])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None); sd = (y - X @ coef).std()
    chk('SYNTH', 'WE6 HST fit of the CSV recovers a2 = %.3f (true %.1f) and a7 = %.3f (true %.1f) within 0.02' % (coef[2], true['a2'], coef[7], true['a7']),
        close(coef[2], true['a2'], 0.02) and close(coef[7], true['a7'], 0.02))
    chk('SYNTH', 'WE6 residual std %.4f ~ generator noise 0.05' % sd, close(sd, 0.05, 0.005))

from collections import Counter
cnt = Counter(c for c, _ in res); fails = [ok for _, ok in res].count(False)
print('\nSUMMARY: %d PASS, %d FAIL  (%s)' % (len(res) - fails, fails, ', '.join('%s %d' % kv for kv in sorted(cnt.items()))))
sys.exit(1 if fails else 0)
