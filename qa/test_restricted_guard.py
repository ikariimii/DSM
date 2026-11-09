"""pytest/CI guard: fail if any repo file is (or is derived from) a dataset whose manifest
usage_restriction is internal-test-only (or that has no manifest: default deny).
Content fingerprints come from /workspace/dam-refs/datasets read by path (cache outside the repo);
without dam-refs only the committed sha256/basename denylist is checked."""
import os, subprocess, sys
import pytest
QA = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(QA)
sys.path.insert(0, QA)
import restricted_guard as g

def test_no_restricted_content_in_repo():
    if os.path.isdir(g.DATASETS) and not os.path.exists(os.path.join(g.CACHE, 'num_windows.u64')):
        g.build()
    hits = g.scan()
    assert not hits, 'restricted data in repo:\n' + '\n'.join('%s -- %s' % h for h in hits)

@pytest.mark.skipif(not os.path.isdir(g.DATASETS), reason='dam-refs not mounted')
def test_guard_detects_a_planted_copy(tmp_path, monkeypatch):
    # self-test: a renamed restricted file and a pasted block of its rows must both be caught
    files, _ = g.restricted_paths()
    def rowy(p):   # a csv whose data rows carry distinctive numeric windows
        try: ls = open(p, errors='ignore').readlines()[3:15]
        except OSError: return False
        return sum(1 for l in ls if any(True for _ in g.windows(l))) >= 6
    src = next(p for p in sorted(files) if p.endswith('.csv') and 2000 < os.path.getsize(p) < 5e7 and rowy(p))
    d = os.path.join(REPO, 'qa', '_guard_selftest'); os.makedirs(d, exist_ok=True)
    try:
        a = os.path.join(d, 'renamed.bin'); open(a, 'wb').write(open(src, 'rb').read())
        b = os.path.join(d, 'pasted.py'); open(b, 'w').write('ROWS = """\n' + ''.join(open(src, errors='ignore').readlines()[3:15]) + '"""\n')
        if not os.path.exists(os.path.join(g.CACHE, 'num_windows.u64')): g.build()
        hit = {os.path.basename(p) for p, _ in g.scan([a, b])}
        assert 'renamed.bin' in hit and 'pasted.py' in hit, hit
    finally:
        for f in os.listdir(d): os.remove(os.path.join(d, f))
        os.rmdir(d)

@pytest.mark.parametrize('script', ['reference_examples.py', 'inclinometer_independent_check.py', 'piezometer_worked_examples.py'])
def test_qa_scripts_pass(script):
    r = subprocess.run([sys.executable, os.path.join(QA, script)], capture_output=True, text=True, cwd=REPO)
    assert r.returncode == 0 and ' FAIL' not in r.stdout.replace('0 FAIL', ''), r.stdout[-2000:] + r.stderr[-2000:]

SCALARS_SRC = os.path.join(g.QA_PRIVATE, 'restricted_derived_scalars.json')

@pytest.mark.skipif(not os.path.exists(SCALARS_SRC), reason='qa_private not mounted')
def test_guard_catches_restricted_derived_scalars():
    # QA-034: values are read from qa_private at runtime (never written into this file); probes live in a
    # temp dir inside the repo and are removed in finally.
    import json
    d = json.load(open(SCALARS_SRC)); g.build_scalars()
    single = d['single'][0]
    grp = next(v for v in d['groups'].values() if len([x for x in v if x not in d['single']]) >= 3)
    pair = [x for x in grp if x not in d['single']][:3]
    pd = os.path.join(REPO, 'qa', '_scalar_selftest'); os.makedirs(pd, exist_ok=True)
    w = lambda n, t: (open(os.path.join(pd, n), 'w').write(t), os.path.join(pd, n))[1]
    try:
        cases = {
            w('single.md', 'dD is %r here\n' % single): True,
            w('reformat.md', '| x | -%s0 |\n' % repr(single)): True,                   # sign + trailing zero
            w('pair.py', 'A = %r; B = %r\n' % (pair[0], pair[1])): True,
            w('spread.md', 'a %r\n\nb %r\n\nc %r\n' % tuple(pair)): True,              # >= 3 of a group per file
            w('one_member.md', 'only %r alone\n' % pair[0]): False,                    # negative control
            w('clean.md', 'tol max(2.0, 3*SE) ranks proposals only\n'): False,
        }
        hit = {p for p, _ in g.scan(list(cases))}
        assert {os.path.relpath(p, REPO) for p, e in cases.items() if e} == hit, hit
    finally:
        for f in os.listdir(pd): os.remove(os.path.join(pd, f))
        os.rmdir(pd)
