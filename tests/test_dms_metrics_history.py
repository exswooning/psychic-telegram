import json
from dms_migrate import cache_metrics


def test_empty_console_read_preserves_timestamped_history_without_claiming_progress(tmp_path):
    path = str(tmp_path / 'metrics.json')
    original = {'status': 'In progress', 'metrics': {'Emails imported': 118628, 'Emails failed': 17}, 'read_at': 100}
    cache_metrics(original, path)
    current = cache_metrics({'status': 'Not started', 'metrics': {}, 'read_at': 200}, path)
    assert current['status'] == 'Not started' and current['metrics'] == {}
    assert current['lastCounters'] == original
    again = cache_metrics({'status': 'unknown', 'metrics': {}, 'read_at': 300}, path)
    assert again['lastCounters'] == original
    assert json.load(open(path)) == again


def test_new_counters_replace_old_without_merging_across_runs(tmp_path):
    path = str(tmp_path / 'metrics.json')
    cache_metrics({'status': 'complete', 'metrics': {'Emails imported': 100}, 'read_at': 100}, path)
    current = {'status': 'In progress', 'metrics': {'Emails discovered': 10}, 'read_at': 200}
    assert cache_metrics(current, path) == current


def test_invalid_cache_does_not_break_fresh_observation(tmp_path):
    path = tmp_path / 'metrics.json'
    path.write_text('broken')
    current = {'status': 'unknown', 'metrics': {}, 'read_at': 100}
    assert cache_metrics(current, str(path)) == current
