"""Create a tiny demonstrator database. Refuses to overwrite an existing file."""
import argparse
import sqlite3
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    path = args.output.resolve()
    if path.exists():
        parser.error(f"File already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, description TEXT, updated_at TEXT, amount REAL)")
        db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", [
            (1, "购买固态硬盘用于本地检索索引", "2026-09-25T10:00:00", 399),
            (2, "购买网线连接测试电脑", "2026-09-25T10:05:00", 29),
            (3, "为开发电脑升级内存", "2026-09-25T10:10:00", 299),
        ])
    print(path)


if __name__ == "__main__":
    main()
