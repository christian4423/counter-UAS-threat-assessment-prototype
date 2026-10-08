"""Shared setup for the integration tests.

main.py has no __main__ guard (it flies on import), so load_main() executes only
the part above "# Start each run": every function plus the live MAVLink
connection, without arming or flying. Tests then wrap the connection's send
methods to drop or record packets.

Needs the SITL container running and UDP 14550 free (main.py not flying).
"""
import sys
from pathlib import Path

MAIN = Path(__file__).resolve().parent.parent / "src" / "main.py"


def load_main():
    src = MAIN.read_text()
    src = src[:src.index("# Start each run")]       # skip the module-level flight
    ns = {"__file__": str(MAIN), "__name__": "main_under_test"}
    exec(compile(src, str(MAIN), "exec"), ns)
    return ns


def run_tests(namespace):
    """Run every test_* function in a test module; exit non-zero on failure."""
    tests = [v for k, v in list(namespace.items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {test.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
