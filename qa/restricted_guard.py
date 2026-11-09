"""Path guard: fail if restricted (internal-test-only) reference data, or content copied from it,
lives in this repo.

Restricted = any dataset row in /workspace/dam-refs/datasets/*/manifest.csv whose usage_restriction
starts with "internal-test-only" (Geotech lead 2026-11-02: internal test only, no redistribution,
nothing shipped; reading by path is OK). Dataset folders WITHOUT a manifest.csv are treated as
restricted (default deny) until the owner adds one.

The repo stores only qa/restricted_denylist.json (sha256 + file basenames, never data). Content
fingerprints (hashes only) are cached OUTSIDE the repo at $DAM_RESTRICTED_FP_CACHE, built by path:
  * sha256 of every restricted file            -> exact copies (any name)
  * basenames of restricted data files         -> renamed-path-free copies
  * 64-bit sha1 prefix of every 4-number window of each  -> embedded data rows / fixtures / pasted tables
    line of restricted CSV/TXT/GK/XLSX/PDF text
  * 64-bit sha1 prefix of normalized text lines >= 40 chars from restricted text/PDFs -> pasted PDF text
  * QA-034: 64-bit hashes of restricted-DERIVED scalars (computed results such as a bias-shift dD, quoted manual
    example values) listed by QA in $DAM_QA_PRIVATE/restricted_derived_scalars.json (outside the repo, read by
    path). A line hits on one 'single' scalar or on >= 2 distinct scalars of one source group; a file hits on
    >= 3 distinct scalars of one group. Sign and trailing zeros are ignored (-1.50 == 1.5).
    The scalar check skips testonly_restricted/ ONLY while git ignores it (sanctioned local-only internal tests).

Usage:
  python qa/restricted_guard.py build            # refresh fingerprints from dam-refs (by path)
  python qa/restricted_guard.py scan [files...]  # scan repo (or given files); exit 1 on hits
  pytest qa/test_restricted_guard.py             # same scan as a test (CI)
"""
import csv, glob, hashlib, json, os, re, subprocess, sys, zipfile

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
DATASETS = os.environ.get('DAM_REFS_DATASETS', '/workspace/dam-refs/datasets')
FP = os.path.join(REPO, 'qa', 'restricted_denylist.json')        # committed: sha256 + basenames only
CACHE = os.environ.get('DAM_RESTRICTED_FP_CACHE', '/workspace/.cache/dam-safety-restricted-fp')  # OUTSIDE repo
QA_PRIVATE = os.environ.get('DAM_QA_PRIVATE', '/workspace/dam-refs/qa_private')                   # OUTSIDE repo
SCALARS = 'derived_scalars.json'   # cache file: hashes only
BIG = 200000   # files with more numeric windows than this are subsampled 1/16 (any pasted block >~50 rows still hits)
SKIP_DIRS = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'}
SKIP_FILES = {os.path.normpath(FP)}
import array
GENERIC = {'derived', 'docs', 'references', 'raw', 'txt', 'web'}
DATA_EXT = {'.csv', '.gk', '.xlsx', '.xls', '.txt', '.json', '.mseed', '.tenv3', '.pdf', '.png', '.zip', '.tar', '.docx', '.stationxml', '.geojson', '.html', '.dat'}
NUM = re.compile(r'(?<![\w.])[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?(?![\w])')   # '-' only as a sign, not in dates/ids
NO_CONTENT = {'.md', '.py', '.lock', '.pyc', '.sha256', '.cff', '.m', '.gnu', ''}   # docs/scripts: sha only, no content fingerprint
MIN_SHA_BYTES = 64
GENERIC_NAMES = {'readme.md', 'readme.txt', 'read_me.txt', 'license', 'citation.cff', 'bagit.txt', 'bag-info.txt',
                 'manifest-md5.txt', 'tagmanifest-md5.txt', 'raw_data.xlsx', 'event.geojson', 'datacite_metadata.json',
                 'mendeley_metadata.json', 'formulation.txt', 'displacements.xlsx', 'manifest.csv'}   # too generic for a name-only hit
W = 4

def h(s): return int.from_bytes(hashlib.sha1(s.encode()).digest()[:8], 'little')
def sha256(p):
    d = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''): d.update(b)
    return d.hexdigest()
def norm_num(t):
    try: x = float(t)
    except ValueError: return None
    if x != x or x in (float('inf'), float('-inf')): return None
    return ('%.6g' % x).replace('+', '')
def sig(v):   # significant digits of a normalised token
    return len(v.lstrip('-').split('e')[0].replace('.', '').strip('0'))
def trivial(win, raw=None):
    # A window is distinctive if it has >=3 tokens with >=3 significant digits (not calendar years),
    # or (low-precision instrument rows, e.g. 0.020 -0.030) all tokens are written with >=2 decimals
    # and >=3 are distinct and non-zero.
    if len(set(win)) < 3: return True
    good = [v for v in win if sig(v) >= 3 and not re.fullmatch(r'(19|20)\d\d', v)]
    if len(good) >= 3: return False
    if raw and all(re.search(r'\.\d\d', t) for t in raw) and len({v for v in win if float(v) != 0}) >= 3: return False
    return True
def windows(line):
    pairs = [(n, t) for n, t in ((norm_num(t), t) for t in NUM.findall(line)) if n is not None]
    toks = [n for n, _ in pairs]; raws = [t for _, t in pairs]
    for i in range(len(toks) - W + 1):
        w = toks[i:i + W]
        if not trivial(w, raws[i:i + W]): yield h(' '.join(w))
def numseq(line):
    # QA-029: separator-independent row fingerprint. The full sequence of numbers on a line (>= 6 numbers, >= 3 distinct
    # non-zero), so a pasted row still hits after '# ' / indent / tab / markdown-table reformatting, including
    # low-precision integer rows (e.g. 12 -8) that windows() treats as trivial. Dropping or reordering columns evades it.
    toks = [n for n in (norm_num(t) for t in NUM.findall(line)) if n is not None]
    if len(toks) >= 6 and len({v for v in toks if float(v) != 0}) >= 3: return h('N:' + ' '.join(toks))
    return None
def scalar_key(t):
    v = norm_num(t)
    return None if v is None else h('S:' + v.lstrip('-'))
def line_scalars(line):
    return {k for k in (scalar_key(t) for t in NUM.findall(line)) if k is not None}
def build_scalars():
    src = os.path.join(QA_PRIVATE, 'restricted_derived_scalars.json')
    if not os.path.exists(src): return None
    d = json.load(open(src))
    out = {'single': sorted({scalar_key(repr(x)) for x in d.get('single', [])}),
           'groups': [sorted({scalar_key(repr(x)) for x in g}) for g in d.get('groups', {}).values()]}
    os.makedirs(CACHE, exist_ok=True); json.dump(out, open(os.path.join(CACHE, SCALARS), 'w'))
    return out
def load_scalars():
    p = os.path.join(CACHE, SCALARS)
    if not os.path.exists(p): return set(), []
    d = json.load(open(p)); return set(d['single']), [set(g) for g in d['groups']]
def norm_text(line):
    s = re.sub(r'\s+', ' ', line).strip().lower()
    return s if len(s) >= 40 and re.search(r'[a-z]{3}', s) else None

def text_lines(p):
    ext = os.path.splitext(p)[1].lower()
    try:
        if ext == '.pdf':
            return subprocess.run(['pdftotext', '-layout', p, '-'], capture_output=True, text=True, timeout=300).stdout.splitlines()
        if ext in ('.xlsx', '.xlsm'):
            out = []
            with zipfile.ZipFile(p) as z:
                for n in z.namelist():
                    if n.startswith('xl/worksheets/') and n.endswith('.xml'):
                        x = z.read(n).decode('utf8', 'ignore')
                        for row in re.findall(r'<row[^>]*>(.*?)</row>', x, re.S):
                            out.append(' '.join(re.findall(r'<v>([^<]*)</v>', row)))
            return out
        if ext in ('.zip', '.tar', '.mseed', '.png', '.docx', '.xls'):
            return []
        with open(p, 'rb') as f: b = f.read(50 << 20)
        if b'\0' in b[:4096]: return []
        return b.decode('utf8', 'ignore').splitlines()
    except Exception:
        return []

def restricted_paths():
    roots, info = set(), []
    for m in sorted(glob.glob(os.path.join(DATASETS, '*', 'manifest.csv'))):
        d = os.path.dirname(m)
        for r in csv.DictReader(open(m, encoding='utf8', errors='ignore')):
            u = (r.get('usage_restriction') or '').strip()
            if not u.lower().startswith('internal-test-only'): continue
            info.append((os.path.basename(d), r.get('dataset_id'), u))
            lf = re.sub(r'\(.*?\)', ' ', r.get('local_files') or '')
            for t in re.split(r'[;\s]+', lf):
                t = t.strip(',')
                if not t: continue
                p = os.path.normpath(os.path.join(d, t))
                for g in (glob.glob(p) if any(c in p for c in '*{') else ([p] if os.path.exists(p) else [])):
                    par = os.path.dirname(g)
                    inside = os.path.normpath(par).startswith(os.path.normpath(d) + os.sep)   # never widen outside the dataset folder
                    if os.path.isfile(g) and inside and os.path.basename(par) not in GENERIC:
                        g = par                       # whole sample folder (e.g. vendor_manual_samples/)
                    roots.add(g)
    for d in sorted(glob.glob(os.path.join(DATASETS, '*/'))):
        if not os.path.exists(os.path.join(d, 'manifest.csv')):
            roots.add(os.path.normpath(d)); info.append((os.path.basename(os.path.normpath(d)), '(no manifest.csv)', 'UNKNOWN -> default deny'))
    for p in glob.glob(os.path.join(DATASETS, '**', '*INTERNAL_ONLY*'), recursive=True): roots.add(p)
    files = set()
    for r in roots:
        if os.path.isfile(r): files.add(r)
        else:
            for dp, dn, fn in os.walk(r):
                files.update(os.path.join(dp, f) for f in fn)
    return sorted(files), info

def build():
    files, info = restricted_paths()
    shas, names, wins, lines, nseq = set(), set(), set(), set(), set()
    for p in files:
        if os.path.getsize(p) >= MIN_SHA_BYTES: shas.add(sha256(p))
        if os.path.splitext(p)[1].lower() in NO_CONTENT or os.path.basename(p).lower() in ('sha256sums', 'license'): continue
        bn = os.path.basename(p).lower()
        if os.path.splitext(p)[1].lower() in DATA_EXT and bn not in GENERIC_NAMES and len(os.path.splitext(bn)[0]) >= 6:
            names.add(os.path.basename(p).lower())
        fw = set()
        for l in text_lines(p):
            fw.update(windows(l))
            t = norm_text(l)
            if t: lines.add(h(t))
            q = numseq(l)
            if q is not None: nseq.add(q)
        if len(fw) > BIG: fw = {w for w in fw if w % 16 == 0}
        wins |= fw
    json.dump({'note': 'hashes and file names only; no restricted data. Rebuild: python qa/restricted_guard.py build',
               'restricted_datasets': info, 'n_files': len(files), 'sha256': sorted(shas), 'basenames': sorted(names)},
              open(FP, 'w'), indent=1)
    os.makedirs(CACHE, exist_ok=True)
    for name, st in (('num_windows.u64', wins), ('text_lines.u64', lines), ('num_lines.u64', nseq)):
        a = array.array('Q', sorted(st)); open(os.path.join(CACHE, name), 'wb').write(a.tobytes())
    sc = build_scalars()
    print('derived scalars: %s' % ('%d single, %d groups' % (len(sc['single']), len(sc['groups'])) if sc else 'none (%s absent)' % QA_PRIVATE))
    print('denylist: %d files, %d names -> %s; content cache: %d windows, %d text lines, %d number rows -> %s' % (len(files), len(names), FP, len(wins), len(lines), len(nseq), CACHE))

def load_cache(name):
    a = array.array('Q'); a.frombytes(open(os.path.join(CACHE, name), 'rb').read()); return set(a)

LOCAL_ONLY = ('testonly_restricted/',)
def git_ignored(rel):
    try: return subprocess.run(['git', '-C', REPO, 'check-ignore', '-q', rel]).returncode == 0
    except Exception: return False

def repo_files():
    for dp, dn, fn in os.walk(REPO):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        for f in fn:
            p = os.path.normpath(os.path.join(dp, f))
            if p not in SKIP_FILES: yield p

def scan(paths=None, min_windows=3):
    fp = json.load(open(FP))
    shas, names = set(fp['sha256']), set(fp['basenames'])
    have_cache = os.path.exists(os.path.join(CACHE, 'num_windows.u64'))
    wins = load_cache('num_windows.u64') if have_cache else set()
    tl = load_cache('text_lines.u64') if have_cache else set()
    nl = load_cache('num_lines.u64') if have_cache and os.path.exists(os.path.join(CACHE, 'num_lines.u64')) else set()
    if have_cache and not nl: print('NOTE: no num_lines cache (built before QA-029): run build to enable row-sequence checks')
    if not have_cache: print('NOTE: no content cache at %s (dam-refs absent?): hash/name/path checks only' % CACHE)
    s_single, s_groups = load_scalars()
    if have_cache and not s_groups: print('NOTE: no derived-scalar cache (QA-034): run build with qa_private present')
    hits = []; _noted = set()
    for p in (paths or list(repo_files())):
        if not os.path.isfile(p): continue
        rel = os.path.relpath(p, REPO)
        if re.search(r'(^|/)(dam-refs|datasets)(/|$)', rel): hits.append((rel, 'path inside a dam-refs/datasets copy'))
        if os.path.basename(p).lower() in names: hits.append((rel, 'basename of a restricted file'))
        if os.path.getsize(p) >= MIN_SHA_BYTES and sha256(p) in shas: hits.append((rel, 'exact copy of a restricted file')); continue
        if not have_cache: continue
        nw, nt, nq, ns, ex = 0, 0, 0, 0, []
        file_sc = set()
        local_ok = rel.replace(os.sep, '/').startswith(LOCAL_ONLY) and git_ignored(rel)
        if local_ok and s_groups: _noted.add(rel.replace(os.sep, '/').split('/')[0])
        for i, l in enumerate(text_lines(p), 1):
            ls = line_scalars(l) if s_groups and not local_ok else set()
            if ls:
                file_sc |= ls
                if ls & s_single or any(len(ls & g) >= 2 for g in s_groups): ns += 1; ex.append(i)
            k = [w for w in windows(l) if w in wins]
            if k: nw += 1; ex.append(i)
            t = norm_text(l)
            if t and h(t) in tl: nt += 1; ex.append(i)
            q = numseq(l)
            if q is not None and q in nl: nq += 1; ex.append(i)
        if not ns and any(len(file_sc & g) >= 3 for g in s_groups): ns = 1
        if nw >= min_windows or nt >= 1 or nq >= 1 or ns >= 1:
            hits.append((rel, 'content derived from restricted data: %d numeric-row lines, %d text lines, %d number rows, %d derived-scalar lines (e.g. lines %s)' % (nw, nt, nq, ns, sorted(set(ex))[:8])))
    for d in sorted(_noted): print('INFO: %s/ is git-ignored local-only: derived-scalar check not applied (other checks are)' % d)
    return hits

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'build': build(); sys.exit(0)
    if os.path.isdir(DATASETS) and (not os.path.exists(FP) or not os.path.exists(os.path.join(CACHE, 'num_windows.u64')) or '--refresh' in sys.argv): build()
    args = [os.path.abspath(a) for a in sys.argv[2:] if not a.startswith('--')] if len(sys.argv) > 1 and sys.argv[1] == 'scan' else None
    hits = scan(args)
    for r, why in hits: print('RESTRICTED  %s  -- %s' % (r, why))
    print('%d hit(s)' % len(hits)); sys.exit(1 if hits else 0)
