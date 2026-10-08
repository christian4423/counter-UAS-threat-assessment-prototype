"""Fault-injection tests for send_command()'s MAVLink command retries.

Integration tests: they need the SITL container running (`docker compose up -d`)
and UDP 14550 free, so don't run them while main.py is flying.

    src/.venv/bin/python tests/test_send_command.py

main.py has no __main__ guard (it flies on import), so this loads only the part
above "# Start each run" - all the functions and the MAVLink connection - and
then wraps command_long_send to drop chosen packets, simulating UDP loss.
"""
import sys
import time
from pathlib import Path

MAIN = Path(__file__).resolve().parent.parent / "src" / "main.py"

src = MAIN.read_text()
src = src[:src.index("# Start each run")]       # skip the module-level flight
ns = {"__file__": str(MAIN), "__name__": "send_command_tests"}
exec(compile(src, str(MAIN), "exec"), ns)

master, send_command, mavlink = ns["master"], ns["send_command"], ns["mavutil"].mavlink
real_send = master.mav.command_long_send

# A harmless command to exercise the protocol: re-requesting a stream rate the
# vehicle is already sending is idempotent, so retries can't change its state.
CMD = mavlink.MAV_CMD_SET_MESSAGE_INTERVAL
ARGS = (mavlink.MAVLINK_MSG_ID_GLOBAL_POSITION_INT, 250000)


def run(drop_first, **kwargs):
    """Call send_command while dropping the first `drop_first` sends.
    Returns (result or exception, [(seconds since start, confirmation), ...])."""
    sends = []
    t0 = time.time()

    def lossy_send(ts, tc, cmd, confirmation, *params):
        sends.append((time.time() - t0, confirmation))
        if len(sends) <= drop_first:
            return                                # datagram "lost"
        real_send(ts, tc, cmd, confirmation, *params)

    master.mav.command_long_send = lossy_send
    try:
        result = send_command(CMD, *ARGS, **kwargs)
    except TimeoutError as e:
        result = e
    finally:
        master.mav.command_long_send = real_send
    return result, sends


def test_no_loss():
    result, sends = run(drop_first=0)
    assert result == "MAV_RESULT_ACCEPTED", result
    assert [c for _, c in sends] == [0], sends


def test_recovers_after_two_drops():
    result, sends = run(drop_first=2)
    assert result == "MAV_RESULT_ACCEPTED", result
    assert [c for _, c in sends] == [0, 1, 2], "confirmation must count resends"
    gaps = [b[0] - a[0] for a, b in zip(sends, sends[1:])]
    assert all(g >= 0.9 for g in gaps), f"resends too fast for a radio link: {gaps}"


def test_gives_up_after_max_retries():
    result, sends = run(drop_first=99)
    assert isinstance(result, TimeoutError), result
    assert [c for _, c in sends] == [0, 1, 2, 3, 4, 5], sends


def test_unsafe_command_is_never_resent():
    # A lost ACK for a non-idempotent command (e.g. takeoff) must not abort the
    # caller: send_command returns None and the caller confirms by vehicle state.
    result, sends = run(drop_first=99, safe_to_retry=False)
    assert result is None, f"expected None so the caller can check state, got {result!r}"
    assert len(sends) == 1, f"non-idempotent command was resent: {sends}"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
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
