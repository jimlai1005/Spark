import importlib.util
from pathlib import Path

_p = Path(__file__).resolve().parent.parent / "deploy" / "ops" / "filet_alert.py"
_spec = importlib.util.spec_from_file_location("filet_alert", _p)
fa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fa)

OK_UNITS = {"filet-api": "active", "nginx": "active"}


def good() -> dict:
    return {
        "mem_mb": {"available": 900},
        "psi_io": {"some_avg300": 5.0},
        "nginx_499_15m": 0,
        "explore_db_mb": 1000,
    }


def run(sample, age=60.0, units=None, failed=0, state=None, now=1000.0):
    return fa.evaluate(sample, age, units or OK_UNITS, failed, state or {}, now)


def test_healthy_no_alert_state_unchanged():
    st = {}
    msgs, new = run(good(), state=st)
    assert msgs == [] and new == {} and st == {}


def test_old_sample_without_new_fields_tolerated():
    msgs, _ = run({"mem_mb": {"available": 900}})
    assert msgs == []


def test_sample_none():
    msgs, new = run(None, age=0.0)
    assert "sampler_stale" in new and len(msgs) == 1


def test_sample_stale_age():
    _, new = run(good(), age=1201.0)
    assert "sampler_stale" in new


def test_low_memory():
    s = good()
    s["mem_mb"]["available"] = 300
    msgs, new = run(s)
    assert "low_memory" in new and "300" in msgs[0]


def test_io_pressure():
    s = good()
    s["psi_io"]["some_avg300"] = 60
    msgs, new = run(s)
    assert "io_pressure" in new and "60" in msgs[0]


def test_client_timeouts():
    s = good()
    s["nginx_499_15m"] = 11
    msgs, new = run(s)
    assert "client_timeouts" in new and "11" in msgs[0]


def test_db_size():
    s = good()
    s["explore_db_mb"] = 2600
    msgs, new = run(s)
    assert "db_size" in new and "2600" in msgs[0]


def test_unit_down():
    msgs, new = run(good(), units={"filet-api": "failed", "nginx": "active"})
    assert "unit_down:filet-api" in new and "failed" in msgs[0]


def test_failed_units():
    msgs, new = run(good(), failed=2)
    assert "failed_units" in new and "2" in msgs[0]


def test_dedup_then_reminder_after_6h():
    s = good()
    s["mem_mb"]["available"] = 100
    _, st = run(s, now=1000.0)
    msgs, st2 = run(s, state=st, now=1000.0 + 3600)
    assert msgs == [] and st2["low_memory"]["last_sent"] == 1000.0
    msgs, st3 = run(s, state=st2, now=1000.0 + 21600)
    assert len(msgs) == 1 and "仍在" in msgs[0]
    assert st3["low_memory"]["since"] == 1000.0 and st3["low_memory"]["last_sent"] == 22600.0


def test_recovery_and_input_not_mutated():
    st = {"low_memory": {"since": 1.0, "last_sent": 1.0}}
    msgs, new = run(good(), state=st)
    assert new == {} and "recovered" in msgs[0] and "low_memory" in msgs[0]
    assert "low_memory" in st
