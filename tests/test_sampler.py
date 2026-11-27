import json

import sampler as S


def test_sampler_derives_cpu_and_interface_rates_and_appends_jsonl(tmp_path):
    cpu = iter([(100, 50), (200, 100)])
    network = iter([(1000, 2000), (1500, 4000)])
    clock = iter([10.0, 15.0])
    sample = S.Sampler("wlp2s0", cpu_reader=lambda: next(cpu),
                       network_reader=lambda _iface: next(network),
                       clock=lambda: next(clock))
    output = tmp_path / "box.jsonl"
    assert S.sample_to_file(str(output), interval=5, count=2, sampler=sample,
                            sleep=lambda _seconds: None) == 2
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert rows[0]["network"]["tx_bytes_per_sec"] is None
    assert rows[1]["cpu"]["busy_pct"] == 50.0
    assert rows[1]["network"]["rx_bytes_per_sec"] == 100.0
    assert rows[1]["network"]["tx_bytes_per_sec"] == 400.0
    assert rows[1]["network"]["bytes_per_sec"] == 500.0


def test_cpu_reader_parses_linux_aggregate_without_counting_guest_twice(tmp_path):
    stat = tmp_path / "stat"
    stat.write_text("cpu 10 2 5 30 3 1 1 0 7 2\ncpu0 5 1 3 15 1 0 1 0\n")
    assert S.read_cpu_counters(str(stat)) == (52, 33)


def test_interface_reader_names_missing_interface(tmp_path):
    try:
        S.read_interface_counters("wlp2s0", str(tmp_path))
    except OSError as exc:
        assert "wlp2s0" in str(exc)
    else:
        raise AssertionError("missing interface must be an actionable error")
