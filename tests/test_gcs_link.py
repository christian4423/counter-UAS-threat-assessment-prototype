"""Tests for the ground-station side of the link: the 1 Hz GCS heartbeat and the
parameter protocol used to pin the vehicle's GCS failsafe to this script.

Integration tests: they need the SITL container running (`docker compose up -d`)
and UDP 14550 free, so don't run them while main.py is flying.

    src/.venv/bin/python tests/test_gcs_link.py
"""
import time

from harness import load_main, run_tests

ns = load_main()
master, pump, set_param, mavlink = ns["master"], ns["pump"], ns["set_param"], ns["mavutil"].mavlink
SYSID_MYGCS = ns["SYSID_MYGCS"]


def record_heartbeats(seconds):
    """Pump the link for `seconds`, recording every heartbeat main.py sends.
    Returns [(seconds since start, mav_type), ...]."""
    real_send = master.mav.heartbeat_send
    sent = []
    t0 = time.time()

    def recording_send(mav_type, *args, **kwargs):
        sent.append((time.time() - t0, mav_type))
        real_send(mav_type, *args, **kwargs)

    master.mav.heartbeat_send = recording_send
    try:
        while time.time() - t0 < seconds:
            pump()
    finally:
        master.mav.heartbeat_send = real_send
    return sent


def test_heartbeat_is_about_1hz():
    # ArduPilot's GCS failsafe trips after FS_GCS_TIMEOUT (5 s) without one, so
    # heartbeats must keep flowing at ~1 Hz from every wait loop via pump().
    sent = record_heartbeats(6)
    assert len(sent) >= 4, f"expected ~5 heartbeats in 6 s, got {len(sent)}"
    gaps = [b[0] - a[0] for a, b in zip(sent, sent[1:])]
    assert all(0.9 <= g <= 1.6 for g in gaps), f"heartbeat gaps out of range: {gaps}"


def test_heartbeat_identifies_as_this_gcs():
    # The vehicle only counts heartbeats whose header sysid matches SYSID_MYGCS;
    # MAVProxy's 255 heartbeats must not keep the failsafe quiet.
    sent = record_heartbeats(2)
    assert sent, "no heartbeat sent"
    assert all(t == mavlink.MAV_TYPE_GCS for _, t in sent), sent
    assert master.mav.srcSystem == SYSID_MYGCS, \
        f"packets go out as sysid {master.mav.srcSystem}, vehicle expects {SYSID_MYGCS}"
    assert master.mav.srcSystem != 255, "same sysid as MAVProxy: failsafe can't detect this script dying"


def test_set_param_confirms_value():
    # Same value main.py sets before arming, so this changes nothing on the vehicle.
    value = set_param("SYSID_MYGCS", SYSID_MYGCS)
    assert abs(value - SYSID_MYGCS) < 1e-3, value


def test_set_param_retries_after_drops():
    real_send = master.param_set_send
    sends = []

    def lossy_send(name, value, *args, **kwargs):
        sends.append(name)
        if len(sends) <= 2:
            return                                # first two PARAM_SETs "lost"
        real_send(name, value, *args, **kwargs)

    master.param_set_send = lossy_send
    try:
        value = set_param("SYSID_MYGCS", SYSID_MYGCS)
    finally:
        master.param_set_send = real_send
    assert abs(value - SYSID_MYGCS) < 1e-3, value
    assert len(sends) == 3, f"expected 2 drops then success, got {len(sends)} sends"


def test_set_param_unknown_name_times_out():
    # A vehicle ignores PARAM_SET for a name it doesn't have, so no PARAM_VALUE
    # ever confirms it: set_param must give up instead of reporting success.
    t0 = time.time()
    try:
        set_param("NOT_A_PARAM", 1)
    except TimeoutError:
        return
    finally:
        print(f"      (gave up after {time.time() - t0:.1f}s)")
    raise AssertionError("set_param reported success for a parameter that doesn't exist")


if __name__ == "__main__":
    run_tests(globals())
