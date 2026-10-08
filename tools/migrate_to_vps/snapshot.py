"""Шаг 1 переноса: снимок состояния агента на этой машине (запускать ПРИ ОСТАНОВЛЕННОМ боте).

    .venv\\Scripts\\python -m tools.migrate_to_vps.snapshot [путь к users.db]

Кладёт в `data/exports/migration/` архив `agent-state-<время>.tar.gz`:
    users.db        — согласованная копия базы (механизм резервного копирования SQLite, а не
                      копирование файла: копия файла посреди записи бывает битой);
    prices/…        — все файлы прайсов, на которые база может ссылаться;
    manifest.json   — опись: число строк по таблицам, файлы прайсов с размерами и SHA-256.
По описи скрипт выкладки проверяет, что на сервер доехало всё.

**Бот должен быть остановлен.** Снимок работающего бота согласован сам по себе, но всё, что
бот сделает после него, на сервер не попадёт, — а агент, ведущий очередь команд 1С, обязан
быть один: две копии выполнят одну команду дважды.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tarfile
from datetime import datetime
from pathlib import Path

from src.storage import price_files

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "exports" / "migration"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(db_path: Path) -> Path:
    if not db_path.is_file():
        raise SystemExit(f"Нет базы {db_path}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    work = OUT / f"agent-state-{stamp}"
    (work / "prices").mkdir(parents=True, exist_ok=True)

    copy = work / "users.db"
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    target = sqlite3.connect(copy)
    with target:
        source.backup(target)
    source.close()

    # Пути в КОПИИ сразу приводим к прямым слэшам: на сервере это сделает и старт бота
    # (`path_fix.normalize`), но опись и проверка должны говорить на одном языке с ним.
    tables = {}
    for (name,) in target.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
        tables[name] = target.execute(f"SELECT COUNT(*) FROM [{name}]").fetchone()[0]
    referenced = set()
    for table, column in (("price", "file_path"), ("supplier_price_file", "path"),
                          ("deferred_tasks", "file_path"), ("price_queue", "path"),
                          ("active_price", "path")):
        if table in tables:
            target.execute(f"UPDATE OR IGNORE {table} SET {column} = "
                           f"REPLACE({column}, '\\', '/') WHERE {column} LIKE '%\\%'")
            referenced |= {price_files.text(r[0]) for r in
                           target.execute(f"SELECT {column} FROM {table}") if r[0]}
    target.commit()
    target.close()

    folder = db_path.parent / price_files.DIR_NAME
    files = {}
    for file in sorted(folder.iterdir()) if folder.is_dir() else []:
        if file.is_file():
            (work / "prices" / file.name).write_bytes(file.read_bytes())
            files[file.name] = {"bytes": file.stat().st_size, "sha256": _sha(file)}

    names = {Path(p).name for p in referenced}
    missing = sorted(names - set(files))
    manifest = {"created": stamp, "source_db": str(db_path), "tables": tables,
                "prices": files, "referenced": sorted(names), "missing_files": missing}
    (work / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                        encoding="utf-8")

    archive = OUT / f"agent-state-{stamp}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(work, arcname="agent-state")

    print(f"Снимок: {archive} ({archive.stat().st_size // 1024} КБ)")
    print(f"  таблиц {len(tables)}; прайсы: {tables.get('price', 0)}, "
          f"задачи: {tables.get('price_task', 0)}, доступ: {tables.get('allowed_users', 0)}")
    print(f"  файлов прайсов {len(files)}, база ссылается на {len(names)}")
    if missing:
        print(f"  ВНИМАНИЕ: база ссылается на файлы, которых нет на диске: {missing}")
    return archive


if __name__ == "__main__":
    default = ROOT / "data" / "users.db"
    snapshot(Path(sys.argv[1]) if len(sys.argv) > 1 else default)
