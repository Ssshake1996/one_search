from __future__ import annotations

import json
import sys


def main():
    encoder = None
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request["method"] == "extract":
                from .extractors import extract
                value = extract(request["path"], request["max_chars"])
            elif request["method"] == "encode":
                if encoder is None:
                    from .model import Encoder
                    encoder = Encoder(request["model_dir"], request["threads"])
                value = encoder.encode(request["texts"], request.get("query", False))
            elif request["method"] in {"db_inspect", "db_query", "db_documents"}:
                from .databases import DatabaseSource
                source = DatabaseSource(request["config"])
                if request["method"] == "db_inspect":
                    value = source.inspect()
                elif request["method"] == "db_query":
                    value = source.query(request["request"])
                else:
                    value = list(source.iter_documents(request.get("max_rows", 1000)))
            else:
                raise ValueError("unknown worker operation")
            response = {"ok": True, "result": value}
        except Exception as exc:
            response = {"ok": False, "error": type(exc).__name__ + ": " + str(exc)[:300]}
        print(json.dumps(response, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
