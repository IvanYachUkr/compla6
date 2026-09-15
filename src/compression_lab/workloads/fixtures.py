"""Small reproducible synthetic relational data. No external corpus lookup."""
from __future__ import annotations

import base64
import json
from pathlib import Path

from ..util import save, sha
from .relational import pack


def movie_schema() -> dict:
    def col(name, type_="text", nullable=False, derived=None):
        out = {"name": name, "type": type_, "nullable": nullable}
        if derived is not None:
            out["derived"] = derived
        return out

    def fk(column, table):
        return {"columns": [column], "table": table, "references": ["id"]}

    return {"version": 1, "dialect": "csv-lexical-v1", "tables": [
        {"name": "titles", "columns": [col("id", "uint64"),
         col("external_id", derived={"kind": "prefix_id", "source": "id", "prefix": "tt", "width": 7}),
         col("title", nullable=True), col("year", "int64", True),
         col("original_title", nullable=True, derived={"kind": "copy", "source": "title"})],
         "primary_key": ["id"]},
        {"name": "entities", "columns": [col("id", "uint64"), col("name", nullable=True)], "primary_key": ["id"]},
        {"name": "episodes", "columns": [col("id", "uint64"), col("series_id", "uint64"),
          col("season", "int64", True), col("number", "int64", True)], "primary_key": ["id"],
         "foreign_keys": [fk("id", "titles"), fk("series_id", "titles")]},
        {"name": "relationships", "columns": [col("id", "uint64"), col("title_id", "uint64"),
          col("entity_id", "uint64"), col("role", nullable=True)], "primary_key": ["id"],
         "foreign_keys": [fk("title_id", "titles"), fk("entity_id", "entities")]}
    ]}


def movie_bundle(variant: int = 0) -> bytes:
    # The pretty schema is intentional. Canonical equality includes its spelling.
    schema = (json.dumps(movie_schema(), indent=2, ensure_ascii=False) + "\n").encode()
    quoted = b'"A ""quoted"",\r\nTitle"' if variant == 0 else b'"Second\nTitle, with CR\rinside"'
    tables = [
        ("titles", b'007,tt0000007,' + quoted + b',+0000,' + quoted + b'\r\n'
                   b'2,tt0000002,"",-9223372036854775808,\r'
                   b'9,tt0000009,\\N,9223372036854775807,\\N'),
        ("entities", b'3,"An ""entity"""\n1,\\N\r\n5,"\\N"'),
        ("episodes", b'2,7,-0,\\N\r\n'),
        ("relationships", b'001,7,3,"role, primary"\r\n9,2,1,\\N\n4,7,5,""')
    ]
    return pack(schema, tables)


def row(table: str, raw: bytes) -> dict:
    return {"table": table, "record_b64": base64.b64encode(raw).decode("ascii")}


def transaction(index: int, kind: str = "bundle") -> dict:
    """Disjoint synthetic IDs. The first inserted title is also an episode."""
    title, entity, relation = 100 + index, 1000 + index, 10000 + index
    name = f'"Inserted {index}, with ""quote"""'.encode()
    if kind == "relationship":
        records = [row("relationships", f'{relation},7,3,new relation\r\n'.encode())]
    else:
        records = [row("titles", str(title).encode() + b',tt' + str(title).zfill(7).encode()
                       + b',' + name + b',+002026,' + name + b'\r\n'),
                   row("entities", f'{entity},"Entity {index}"\n'.encode()),
                   row("episodes", f'{title},7,1,{index}\r'.encode()),
                   row("relationships", f'{relation},{title},{entity},actor\n'.encode())]
    return {"token": f"synthetic-{kind}-{index}", "records": records}


def create_dataset(root: Path, workload: str) -> Path:
    from ..dataset import example_card
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    rows = []
    for i, split in enumerate(("train", "development")):
        p = root / f"movie-{i}.rlb"
        p.write_bytes(movie_bundle(i))
        rows.append({"alias": f"movie-{i}", "source_group": f"synthetic-{i}", "split": split,
                     "path": p.name, "canonical_bytes": p.stat().st_size, "canonical_sha256": sha(p)})
    save(root / "manifest.json", {"schema_version": 1, "objects": rows})
    card = example_card("synthetic-relational-v1", "bytes")
    card.update(adapter="relational_bundle", workload=workload,
                input_domain="RLB1 checked lexical relational bundles",
                scope="Two tiny synthetic movie databases. Not a corpus benchmark.")
    card["limits"].update(timeout_seconds=20, output_bytes=128 * 1024 * 1024)
    if workload == "mutable_store":
        card["objective"]["encode_floor_bytes_per_second"] = None
        card["mutable_policy"] = {"protocol_version": 1, "single_writer": True,
                                  "insert_only": True, "durability": "process_crash",
                                  "cache_procedure": "fresh_process_then_warm_no_os_cache_drop",
                                  "metric_trials": 5}
    save(root / "dataset-card.json", card)
    return root / "dataset-card.json"


def static_records():
    """Valid RLB1 properties for specialized relational native codecs.

    Unlike the independent-byte workload, arbitrary malformed container bytes
    are not valid encoder inputs under the rlb1-lexical contract.
    """
    from ..stream import StreamRecord
    from .relational import json_bytes
    values = [movie_bundle(0), movie_bundle(1)]
    s = {'version': 1, 'dialect': 'csv-lexical-v1', 'tables': [
        {'name': 'odd\r\ntable', 'columns': [{'name': 'value', 'type': 'bytes', 'nullable': True}]}]}
    for value in (b'', b'\0', bytes(range(256)), b'\xff\xfe\x80', b'a' * 8192,
                  b'\r\n\r\n', b'\\N', b'"', b'ends without newline'):
        payload = b'"' + value.replace(b'"', b'""') + b'"' if value else b''
        values.append(pack(json_bytes(s), [('odd\r\ntable', payload)]))
    numeric = {'version': 1, 'dialect': 'csv-lexical-v1', 'tables': [
        {'name': 'integers', 'columns': [{'name': 'signed', 'type': 'int64', 'nullable': True},
                                        {'name': 'unsigned', 'type': 'uint64', 'nullable': False}]}]}
    values.append(pack(json_bytes(numeric), [('integers', b'-9223372036854775808,18446744073709551615\r\n+0000,-0\r\\N,0000')]))
    records = [StreamRecord(f'relational-{i:03}', data) for i, data in enumerate(values)]
    for name in ('a', 'b-0', 'different-prefix-0123', 'x' * 4096, 'z' * 1048576):
        records.append(StreamRecord(name, values[0]))
    return tuple(sorted(records, key=lambda r: r.alias))
