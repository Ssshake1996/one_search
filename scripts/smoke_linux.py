"""Linux headless installation and lifecycle acceptance on disposable local fixtures."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def main():
    if not sys.platform.startswith('linux'):
        raise SystemExit('This acceptance requires a real Linux environment')
    repo = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='one-search-linux-') as work:
        base = Path(work)
        root, app, data = base/'合成 docs',base/'应用 app',base/'数据 data'
        root.mkdir()
        fixture = root/'search.txt'
        fixture.write_text('headlessfirstneedle',encoding='utf8')
        cfg = data/'config.json'
        def run(command,timeout=240):
            result = subprocess.run(command,capture_output=True,text=True,encoding='utf8',timeout=timeout)
            if result.returncode:
                raise RuntimeError(f'Command failed ({result.returncode}): '+result.stderr[-3000:])
            return result.stdout
        install = ['bash',str(repo/'scripts/install.sh'),'--install-dir',str(app),'--data-dir',str(data),
                   '--root',str(root),'--python',sys.executable,'--skip-model','--no-autostart']
        executable = app/'venv/bin/data-search'
        def cli(*args):
            return json.loads(run([str(executable),*args,'--config',str(cfg)],timeout=90))
        try:
            run(install)
            original = cfg.read_bytes()
            assert cli('installation-status','--install-dir',str(app))['basic_search_ready']
            run(install)
            assert cfg.read_bytes()==original
            deadline = time.monotonic()+20
            while time.monotonic()<deadline:
                hits = cli('search','headlessfirstneedle','--mode','keyword')['results']
                if hits:
                    break
                time.sleep(.25)
            assert hits
            assert cli('diagnose',str(fixture))['code']=='ready'
            cli('pause','--seconds','30')
            fixture.write_text('headlesssecondneedle',encoding='utf8')
            assert cli('refresh',str(fixture))['accepted']
            assert cli('search','headlesssecondneedle','--mode','keyword')['results']
            cli('resume')
            cli('export-config',str(base/'settings.json'))
            cli('stop')
            index = base/'外置 index'
            assert cli('relocate-index',str(index))['changed']
            cli('start')
            assert cli('search','headlesssecondneedle','--mode','keyword')['results']
            run(['bash',str(repo/'scripts/uninstall.sh'),'--install-dir',str(app)])
            assert data.is_dir() and cfg.is_file() and (index/'index.sqlite3').exists()
            run(install)
            assert cli('search','headlesssecondneedle','--mode','keyword')['results']
            extra = index/'unrelated.txt'
            extra.write_text('not an index')
            run(['bash',str(repo/'scripts/uninstall.sh'),'--install-dir',str(app),'--delete-data'])
            assert not app.exists() and not data.exists() and extra.exists() and fixture.exists()
            print(json.dumps({'ok':True,'platform':sys.platform,'python':sys.version.split()[0],
                              'cases':['headless_install','no_model_basic','repeat_preserves_config','diagnose','paused_refresh',
                                       'export','index_relocation','preserve_uninstall','reinstall','delete_managed_only'],
                              'user_files_accessed':False,'autostart_tested':False,'semantic_model_tested':False}))
        finally:
            if executable.exists() and cfg.exists():
                subprocess.run([str(executable),'stop','--config',str(cfg)],capture_output=True,timeout=30)


if __name__=='__main__':
    main()
