from copy import deepcopy

import pytest

from data_search.config import defaults
from data_search.runtime_policy import RuntimePolicy, apply_preset, validate_policy


class Clock:
    value = 100.0

    def __call__(self):
        return self.value


def make_policy(tmp_path, clock, **settings):
    config = defaults(str(tmp_path))
    config['runtime_policy'] = settings
    return RuntimePolicy(config, clock=clock, monotonic=clock, sample=lambda: {'cpu_percent': 0})


def test_preset_preserves_entire_scope_and_disk_contract(tmp_path):
    config = defaults(str(tmp_path), [str(tmp_path / 'source')])
    config['exclude_paths'] = ['sensitive']
    config['resource']['max_disk_mb'] = 700
    original = deepcopy(config)
    for name in ('low', 'balanced', 'fast'):
        changed = apply_preset(config, name)
        for field in ('scope', 'roots', 'exclude_paths', 'indexing', 'databases'):
            assert changed[field] == config[field]
        assert changed['resource']['max_disk_mb'] == 700
    assert config == original


def test_pause_survives_restart_and_expires_while_offline(tmp_path):
    clock = Clock()
    policy = make_policy(tmp_path, clock)
    policy.pause(60)
    clock.value += 20
    restarted = make_policy(tmp_path, clock)
    assert restarted.decision()['reason'] == 'user_pause'
    assert restarted.status()['automatic_wait'] is False
    clock.value += 41
    assert make_policy(tmp_path, clock).decision()['background_allowed']
    assert restarted.status()['user_paused'] is False


def test_indefinite_pause_and_resume_do_not_disable_queries(tmp_path):
    clock = Clock()
    policy = make_policy(tmp_path, clock)
    policy.pause()
    clock.value += 10_000_000
    policy = make_policy(tmp_path, clock)
    assert policy.decision()['query_allowed']
    assert policy.status()['user_paused']
    assert policy.resume()['reason'] is None
    assert policy.decision()['background_allowed']


def test_busy_hysteresis_and_query_priority(tmp_path):
    clock = Clock()
    policy = make_policy(tmp_path, clock, busy_seconds=3)
    assert policy.decision({'cpu_percent': 90})['background_allowed']
    clock.value += 3
    assert policy.decision({'cpu_percent': 90})['reason'] == 'system_busy'
    assert policy.decision({'cpu_percent': 70})['reason'] == 'system_busy'
    assert policy.decision({'cpu_percent': 60})['background_allowed']
    policy.foreground(5)
    assert policy.decision()['reason'] == 'foreground_query'
    clock.value += 6
    assert policy.decision({'cpu_percent': 0})['background_allowed']


def test_battery_retreat_and_return_to_ac(tmp_path):
    clock = Clock()
    policy = make_policy(tmp_path, clock)
    sample = {'cpu_percent': 0, 'on_battery': True, 'battery_percent': 80}
    assert policy.decision(sample)['background_allowed']
    clock.value += 1
    assert policy.decision(sample)['reason'] == 'battery_saving'
    clock.value += 4
    assert policy.decision(sample)['background_allowed']
    assert policy.decision({**sample, 'battery_percent': 10})['reason'] == 'battery_low'
    assert policy.decision({**sample, 'on_battery': False})['background_allowed']


def test_continuous_search_polling_cannot_starve_background(tmp_path):
    clock = Clock()
    policy = make_policy(tmp_path, clock)
    allowed = []
    for tick in range(200):
        policy.foreground()
        if policy.decision({'cpu_percent': 0})['background_allowed']:
            allowed.append(tick)
        clock.value += .05
    assert 2 <= len(allowed) <= 4
    assert allowed[0] <= 61


def test_idle_and_ac_preferences_recover_and_report_unsupported(tmp_path):
    clock = Clock()
    policy = make_policy(tmp_path, clock, idle_only=True, on_ac_only=True)
    assert policy.decision({'cpu_percent': 0, 'on_battery': True, 'idle_seconds': 1000})['reason'] == 'waiting_for_ac_power'
    assert policy.decision({'cpu_percent': 0, 'on_battery': False, 'idle_seconds': 5})['reason'] == 'waiting_for_idle'
    assert policy.decision({'cpu_percent': 0, 'on_battery': False, 'idle_seconds': 500})['background_allowed']
    unsupported = policy.decision({'cpu_percent': 0, 'on_battery': False, 'idle_seconds': None})
    assert unsupported['background_allowed']
    assert unsupported['state_warning'] == 'idle_detection_unavailable_policy_not_enforced'


@pytest.mark.parametrize('value', [True, 0, -1, float('nan'), float('inf'), 604801])
def test_invalid_pause_cannot_poison_persisted_state(tmp_path, value):
    policy = make_policy(tmp_path, Clock())
    with pytest.raises(ValueError):
        policy.pause(value)
    assert not policy.status()['user_paused']


def test_policy_configuration_rejects_inverted_hysteresis():
    with pytest.raises(ValueError):
        validate_policy({'busy_cpu_percent': 50, 'resume_cpu_percent': 60})
