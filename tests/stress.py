"""Stress test: large synthetic databases through the real SQLECmd, timed.

    python tests/stress.py C:\\Tools\\SQLECmd.exe [--calls 1000000 --photos 500000 --apps 20000]

Reports wall time, peak memory (Windows, via the process's peak working set) and checks that
every row and every icon came out.
"""
import argparse
import csv
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_measured(cmd, log_path):
    """Run cmd, sampling resident memory (psutil, if installed) until it exits."""
    try:
        import psutil
    except ImportError:
        psutil = None
    with open(log_path, 'w', encoding='utf-8') as log:
        p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=os.path.dirname(log_path))
        peak = 0
        proc = psutil.Process(p.pid) if psutil else None
        while p.poll() is None:
            if proc:
                try:
                    peak = max(peak, proc.memory_info().rss)
                except psutil.Error:
                    pass
            time.sleep(0.1)
    return p.returncode, (f'{peak / 1e6:.0f}' if proc else 'n/a (pip install psutil)')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('sqlecmd')
    ap.add_argument('--calls', type=int, default=1_000_000)
    ap.add_argument('--photos', type=int, default=500_000)
    ap.add_argument('--apps', type=int, default=20_000)
    a = ap.parse_args()
    work = tempfile.mkdtemp(prefix='pl_stress_')
    try:
        t0 = time.time()
        n = fixtures.build_stress(os.path.join(work, 'in'), a.calls, a.photos, a.apps)
        size = sum(os.path.getsize(p) for p in glob.glob(os.path.join(work, 'in', '*')))
        print(f'built {n} in {time.time() - t0:.1f}s, {size / 1e6:.0f} MB on disk')
        out = os.path.join(work, 'out')
        cmd = [a.sqlecmd, '-d', os.path.join(work, 'in'), '--hunt', '--maps', os.path.join(ROOT, 'maps'),
               '--csv', os.path.join(out, 'csv'), '--blobdir', os.path.join(out, 'blobs')]
        t0 = time.time()
        rc, peak = run_measured(cmd, os.path.join(work, 'console.log'))
        wall = time.time() - t0
        counts = {}
        for f in glob.glob(os.path.join(out, 'csv', '*.csv')):
            with open(f, encoding='utf-8-sig', newline='') as fh:
                counts[os.path.basename(f).split('_', 1)[1]] = sum(1 for _ in csv.reader(fh)) - 1
        blobs = len(glob.glob(os.path.join(out, 'blobs', '*')))
        print(f'SQLECmd exit={rc} wall={wall:.1f}s peak_memory={peak} MB')
        for k, v in sorted(counts.items()):
            print(f'  {k}: {v} rows')
        print(f'  icon files: {blobs}')
        ok = (rc == 0 and any('CallHistory' in k and v == n['calls'] for k, v in counts.items())
              and any(k.startswith('WindowsYourPhone_PhotosDB_') and v == n['photos'] for k, v in counts.items())
              and any('PCAppData' in k and v == n['apps'] for k, v in counts.items())
              and blobs == n['apps'])
        print('RESULT:', 'PASS' if ok else 'FAIL')
        sys.exit(0 if ok else 1)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
