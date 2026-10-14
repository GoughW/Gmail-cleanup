"""Required gate: lock validation, loopback-only policy and full unittest discovery."""
import importlib.metadata
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests/network_guard'))
import sitecustomize  # explicit for this process; PYTHONPATH propagates to spawned Python children
os.environ['PYTHONPATH']=str(ROOT/'tests/network_guard')+os.pathsep+str(ROOT)
def main():
    for line in (ROOT/'requirements.lock').read_text().splitlines():
        if line and not line.startswith('#'):
            name,version=line.split('==')
            if importlib.metadata.version(name)!=version:
                raise SystemExit('Dependency lock mismatch: '+name)
    if sys.version_info[:2]!=(3,12):
        raise SystemExit('Only Python 3.12 is reviewed/supported by this release')
    with tempfile.TemporaryDirectory(prefix='gmail-review-cache-') as cache:
        os.environ['XDG_CACHE_HOME']=cache
        suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'),top_level_dir=str(ROOT))
        result=unittest.TextTestRunner(verbosity=2).run(suite)
        raise SystemExit(0 if result.wasSuccessful() and not result.skipped else 1)


if __name__ == "__main__":
    main()
