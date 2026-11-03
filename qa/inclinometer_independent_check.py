"""QA independent check of Inclinometer Specialist datasets + verify.py expectations.
Re-extracts the source pages directly from the PDFs with `pdftotext -layout` (does NOT use
/workspace/dam-refs/txt or build_from_manual_text.py), compares every transcribed cell, and
recomputes every published reduced value with QA's own formulas."""
import csv, json, math, os, re, subprocess, sys
# Expected strings/values quoted from restricted (internal-test-only) PDFs are NOT embedded here;
# they are read by path from $DAM_QA_PRIVATE (default /workspace/dam-refs/qa_private). Source data is read by path.
PRIV = os.path.join(os.environ.get('DAM_QA_PRIVATE', '/workspace/dam-refs/qa_private'), 'inclinometer_expected.json')
if not os.path.exists(PRIV): print('SKIP: %s not found (restricted reference data is read by path only)' % PRIV); sys.exit(0)
X = json.load(open(PRIV))
R = '/workspace/dam-refs/'; DS = R + 'datasets/inclinometers/vendor_manual_samples/'
res = []
def chk(name, ok, det=''):
    res.append(ok); print(('PASS' if ok else 'FAIL') + '  ' + name + (('  -- ' + det) if det else ''))
def pdf(path, p):
    return subprocess.run(['pdftotext', '-layout', '-f', str(p), '-l', str(p), path, '-'], capture_output=True, text=True).stdout
def rows(path):
    return [r for r in csv.DictReader(l for l in open(path) if not l.startswith('#'))]
N = lambda s: float(s.replace('+', ''))

# ---------- 1. Slope Indicator DMM DEMO hole 45 (DMM PDF p100 deviation table, p102 diff table) ----------
DMM = R + 'z-dos-digitilt-datamate-and-dos-dmm.pdf'
t100 = {N(l.split()[0]): l.split() for l in pdf(DMM, 100).splitlines() if re.match(r'\s+\d+\.000\s', l)}
t102 = {N(l.split()[0]): l.split() for l in pdf(DMM, 102).splitlines() if re.match(r'\s+\d+\.000\s', l)}
C = rows(DS + 'sinco_dmm_demo_hole45_A.csv')
chk('DMM rows: PDF p100=%d p102=%d csv=%d' % (len(t100), len(t102), len(C)), len(t100) == len(t102) == len(C) == X['dmm_rows'])
mism = []
for r in C:
    d = N(r['depth_ft']); a, b = t100[d], t102[d]
    want = dict(init_A0=a[1], init_A180=a[2], pub_init_incr_dev_in=a[3], cur_A0=a[4], cur_A180=a[5],
                pub_cur_incr_dev_in=a[6], pub_incr_disp_in=a[7], pub_cum_disp_in=a[8],
                pub_init_diff=b[3], pub_cur_diff=b[6], pub_change=b[7])
    if b[1] != a[1] or b[2] != a[2] or b[4] != a[4] or b[5] != a[5]: mism.append(('p100!=p102 raw', d))
    for k, v in want.items():
        if N(r[k]) != N(v): mism.append((k, d, r[k], v))
chk('DMM transcription: every CSV cell == PDF cell (11 cols x 30 depths)', not mism, str(mism[:5]))
# recompute from raw (L=24 in, K=20000, D=A0-A180, dev=L*D/(2K), cum bottom-up)
L, K = 24.0, 20000.0
bad = []; inc = []
ds = sorted(t100)                                    # 2..60 ft, top->bottom
for d in ds:
    a = t100[d]; i0, i1, c0, c1 = (int(x) for x in (a[1], a[2], a[4], a[5]))
    Di, Dc = i0 - i1, c0 - c1
    if Di != int(t102[d][3]) or Dc != int(t102[d][6]) or Dc - Di != int(t102[d][7]): bad.append(('diff/change', d))
    devi, devc = L * Di / (2 * K), L * Dc / (2 * K)
    if round(devi, 4) != N(a[3]) or round(devc, 4) != N(a[6]): bad.append(('dev', d, devi, devc))
    inc.append(devc - devi)
    if abs((devc - devi) - N(a[7])) > 5e-5: bad.append(('incr', d))
cum = [sum(inc[i:]) for i in range(len(inc))]
for d, c in zip(ds, cum):
    if abs(c - N(t100[d][8])) > 5e-5: bad.append(('cum', d, round(c, 5), t100[d][8]))
chk('DMM recompute from raw: DIFF, CHANGE, INCR.DEV (4dp exact), INCR DISP, CUM DISP (bottom-up)', not bad, str(bad[:5]))
# p102 prose worked example
p102 = pdf(DMM, 102)
chk('DMM p102 prose worked example strings present in PDF', all(s in p102 for s in X['dmm_p102_strings']))
e = X['dmm_p102_example']
chk('DMM p102 prose recompute: L*half_change/K = published', abs(e['L'] * e['half_change'] / e['K'] - e['expected_in']) < 1e-12)

# ---------- 2. DMM intro examples (verify.py T2, cited "PDF p11-12") ----------
intro = pdf(DMM, 11) + pdf(DMM, 12)
found = {s: (s in intro) for s in X['dmm_intro_tokens_reported']}
chk('DMM intro p11-12: example numbers present in PDF', all(found[s] for s in X['dmm_intro_tokens_required']), str(found))

# ---------- 3. Geokon GK-603 IH01 (PDF p50 raw files, p51 checksum, p52 deflection, p56 profile; App E p61-62) ----------
GKP = R + 'Geokon_GK-603.pdf'
Lr, Rr = {}, {}
for l in pdf(GKP, 50).splitlines():
    m = re.findall(r'(\d+) ,\s*(-?\d+) ,\s*(-?\d+) ,\s*(-?\d+) ,\s*(-?\d+)', l)
    if len(m) == 2:
        Lr[float(m[0][0])] = tuple(map(int, m[0][1:])); Rr[float(m[1][0])] = tuple(map(int, m[1][1:]))
def gkfile(fn):
    o = {}
    for l in open(DS + fn):
        p = [x.strip() for x in l.split(',')]
        if len(p) == 5 and p[0].isdigit(): o[float(p[0])] = tuple(int(x) for x in p[1:])
    return o
I, P = gkfile('geokon_gk603_IH01_TEST1.GK'), gkfile('geokon_gk603_IH01_TEST2.GK')
chk('GK-603 raw .GK files == PDF p50 (51 levels x 4 readings, both files)', I == Lr and P == Rr and len(I) == X['gk603_levels'],
    'levels pdf=%d/%d' % (len(Lr), len(Rr)))
def rep(p):
    o = {}
    for l in pdf(GKP, p).splitlines():
        if re.match(r'\s*\+[45]\d\d\.0\s', l): a = l.split(); o[N(a[-1])] = a
    return o
c51, d52, p56 = rep(51), rep(52), rep(56)
G = rows(DS + 'geokon_gk603_IH01_A_published.csv')
mism = []
for g in G:
    z = N(g['level_ft']); a, b, c = c51[z], d52[z], p56[z]
    want = dict(pub_A_sum_TEST1=a[3], pub_A_sum_TEST2=a[6], pub_A_shift=a[7], pub_A_diff_TEST1=b[3], pub_A_diff_TEST2=b[6],
                pub_A_change=b[7], pub_A_defl_in=b[8], pub_A_corr_diff_half_TEST1=c[4], pub_A_profile_in_TEST1=c[5])
    for k, v in want.items():
        if N(g[k]) != N(v): mism.append((k, z, g[k], v))
    # report raw columns must equal raw files (A+ A- of TEST1/TEST2)
    if (N(b[1]), N(b[2]), N(b[4]), N(b[5])) != (I[z][0], I[z][1], P[z][0], P[z][1]): mism.append(('report raw != file', z))
chk('GK-603 published CSV == PDF p51/p52/p56 cells (9 cols x 51 levels) and report raw == .GK files', not mism and len(G) == X['gk603_levels'], str(mism[:5]))
# recompute (Eq E-1/E-2 deflection M=0.0003 in/ft, RINT=2; Eq E-3/E-4 profile M=0.0006; ZZ=0; bottom-up)
lv = sorted(I); bad = []
SA = [(P[z][0] - P[z][1]) - (I[z][0] - I[z][1]) for z in lv]
CA = [0.0003 * 2 * s for s in SA]; defl = [sum(CA[i:]) for i in range(len(CA))]
PR = [0.0006 * 2 * (I[z][0] - I[z][1]) / 2 for z in lv]; prof = [sum(PR[i:]) for i in range(len(PR))]
for z, d, pr in zip(lv, defl, prof):
    a, b, c = c51[z], d52[z], p56[z]
    if I[z][0] + I[z][1] != N(a[3]) or P[z][0] + P[z][1] != N(a[6]) or N(a[6]) - N(a[3]) != N(a[7]): bad.append(('sum/shift', z))
    if abs(d - N(b[8])) > 5e-5: bad.append(('defl', z, round(d, 4), b[8]))
    if abs(pr - N(c[5])) > 5e-5: bad.append(('prof', z, round(pr, 4), c[5]))
    if abs(abs((I[z][0] - I[z][1]) / 2) - abs(N(c[4]))) > 0.5: bad.append(('diff/2', z))
chk('GK-603 recompute: checksum, shift, deflection (E-1/E-2), profile (E-3/E-4), bottom-up', not bad, str(bad[:5]))
appE = pdf(GKP, 61) + pdf(GKP, 62)
chk('GK-603 App E multipliers (defl, profile) present in PDF', all(m in appE for m in X['gk603_appE_multipliers']))
chk('GK-603 multiplier derivation: 0.0003 in/ft = 12/(2*20000) (2sin units: 10000 @ 30deg -> K=20000)',
    abs(12 / 40000 - 0.0003) < 1e-12 and abs(2 * math.sin(math.radians(30)) * 10000 - 10000) < 1e-9)

# ---------- 4. Geokon 6150 IPI sample (PDF p17; Eq 3 & Eq 5 on p14-15) ----------
IPI = R + 'inclinometers/Geokon_6150_IPI.pdf'
t = pdf(IPI, 17).splitlines()
top = [l.split() for l in t if re.match(r'\s+[1-8]\s+\d\s+\d+\s+6\d\d\s', l)]
bot = [l.split() for l in t if re.match(r'\s+[1-8]\s+0\.\d+\s+\d+\s+0\.', l)]
S = rows(DS + 'geokon_6150_ipi_sample.csv')
mism = [(s['sensor'], k) for s, a, b in zip(S, top, bot)
        for k, v in zip(list(s.keys()), a + b[1:]) if float(s[k]) != float(v)]
chk('6150 CSV == PDF p17 table (8 sensors x 13 cols)', len(S) == len(top) == len(bot) == X['ipi_sensors'] and not mism, str(mism[:5]))
bad, gl = [], []
for a, b in zip(top, bot):
    Lm, G6, R0, T0 = float(a[1]), float(a[4]), float(a[5]), float(a[6]); R1, T1 = float(b[1]), float(b[2])
    r1c = R1 - 0.0003 * (T1 - T0); dR = r1c - R0; g = G6 * Lm * 1000 * dR; gl.append(g)
    if abs(r1c - float(b[3])) > 5e-5: bad.append(('R1corr', a[0]))
    if abs(dR - float(b[4])) > 5e-5: bad.append(('dR', a[0]))
    if abs(g - float(b[5])) > 0.005: bad.append(('GLdR', a[0], round(g, 3)))
acc = [sum(gl[i + 1:]) for i in range(8)]
for b, x in zip(bot, acc):
    if abs(x - float(b[6])) > 0.015: bad.append(('acc', b[0], round(x, 3), b[6]))
chk('6150 recompute: R1corr (Eq3), dR, G*L*dR (Eq5), Acc Defl, published surface value', not bad and abs(sum(gl) - X['ipi_surface_mm']) < 0.01 and str(X['ipi_surface_mm']) in pdf(IPI, 17), str(bad))
p14 = pdf(IPI, 14) + pdf(IPI, 15)
chk('6150 Eq3 temperature-coefficient text and arc-second note present (source inconsistency noted by specialist)', '0.0003' in p14 and 'arc' in p14)

# ---------- 5. Constants / corrections cited elsewhere ----------
DR = R + 'data_reduction.pdf'
dr = ''.join(pdf(DR, p) for p in (1, 2, 3))
chk('data_reduction p2-3 worked-example values present', all(s in dr for s in X['data_reduction_tokens']))
BS = R + 'inclinometers/DGSI_Bias_Shift.pdf'
bs = ''.join(pdf(BS, p) for p in range(1, 9))
chk('DGSI_Bias_Shift unit divisors present', all(s in bs for s in X['bias_shift_tokens']))
RO = R + 'inclinometers/DGSI_Rotation.pdf'
ro = ''.join(pdf(RO, p) for p in range(1, 7))
x, deg = X['rotation_tokens']
chk('DGSI_Rotation example present and asin(x) reproduces published degrees', x in ro and deg in ro and abs(math.degrees(math.asin(float(x))) - float(deg)) < 0.005)
print('\nSUMMARY: %d PASS, %d FAIL' % (res.count(True), res.count(False)))
sys.exit(1 if False in res else 0)
