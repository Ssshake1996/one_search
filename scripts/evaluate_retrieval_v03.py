"""Create a fresh synthetic corpus, then run the unchanged 35 + 4 new cases."""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from create_demo import create_demo
from evaluate_search import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', required=True, type=Path, help='New or empty synthetic workspace')
    parser.add_argument('--model', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    work = args.work.resolve()
    if work.exists() and (not work.is_dir() or any(work.iterdir())):
        parser.error('--work must be new or empty')
    project = Path(__file__).resolve().parent.parent
    base = json.loads((project/'tests/evaluation_queries.json').read_text(encoding='utf-8'))
    extra = json.loads((project/'tests/evaluation_queries_v03.json').read_text(encoding='utf-8'))
    demo = work/'synthetic'
    create_demo(demo)
    for relative, text in extra['fixtures'].items():
        target = (demo/relative).resolve()
        if not target.is_relative_to(demo.resolve()/'files'):
            raise ValueError('Synthetic fixture must stay inside files/')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text,encoding='utf-8')
    base['cases'].extend(extra['cases'])
    cases = work/'cases.json'
    cases.write_text(json.dumps(base,ensure_ascii=False,indent=2),encoding='utf-8')
    run(SimpleNamespace(demo=str(demo), data=str(work/'data'), model=args.model,
                        cases=str(cases), output=args.output))


if __name__ == '__main__':
    main()
