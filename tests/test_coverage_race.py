from data_search.config import defaults
from data_search.engine import Engine
import data_search.engine as engine_module


def test_coverage_retains_snapshot_when_background_invalidates_during_read(tmp_path, monkeypatch):
    root = tmp_path / 'files'
    root.mkdir()
    (root / 'fixture.txt').write_text('Synthetic coverage evidence', encoding='utf-8')
    config = defaults(str(tmp_path / 'data'), [str(root)])
    config['semantic']['enabled'] = False
    config['resource']['batch_sleep_ms'] = 0
    engine = Engine(config)
    try:
        engine.scan_once()
        expected = engine.coverage()
        clock = engine_module.time.monotonic

        def invalidate_at_age_check():
            # A scan can invalidate the shared cache after a status request has
            # accepted it, before that request constructs its response.
            engine._coverage_cache = None
            return clock()

        with monkeypatch.context() as context:
            context.setattr(engine_module.time, 'monotonic', invalidate_at_age_check)
            actual = engine.coverage()
        assert actual == expected
        assert actual['chunks'] == 1
        assert engine._coverage_cache is None
        assert engine.coverage()['chunks'] == 1
    finally:
        engine.close()
