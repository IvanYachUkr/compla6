"""Standalone tiny durable candidate for the synthetic movie-store contract.

Only Python's standard library is imported. This file is copied into a candidate
snapshot and run in the sandbox; the evaluator never imports it as its oracle.
The format favors auditability, not size or speed. Process-crash tests are not a
power-loss claim. All writable state, including test markers, stays in cwd.
"""
import base64
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import struct
import sys
import time

MAX_REQUEST = 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
MAX_EXPORT = 5 * 1024 * 1024
MAX_STATE = 32 * 1024 * 1024
TOKEN = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
FIELD = re.compile(r'(?:"((?:[^"]|"")*)"|([^",\r\n]*))(,|\r\n|\r|\n|$)', re.DOTALL)
INTEGER = re.compile(r'[+-]?[0-9]+\Z')
TABLES = ('titles', 'entities', 'episodes', 'relationships')
COLUMNS = {
    'titles': (('id', 'uint64', False), ('external_id', 'text', False), ('title', 'text', True),
               ('year', 'int64', True), ('original_title', 'text', True)),
    'entities': (('id', 'uint64', False), ('name', 'text', True)),
    'episodes': (('id', 'uint64', False), ('series_id', 'uint64', False), ('season', 'int64', True), ('number', 'int64', True)),
    'relationships': (('id', 'uint64', False), ('title_id', 'uint64', False), ('entity_id', 'uint64', False), ('role', 'text', True)),
}
FAULTS = ('wal_short_write', 'wal_fsync', 'wal_after_fsync', 'merge_snapshot_fsync',
          'merge_before_manifest', 'manifest_rename', 'directory_fsync', 'merge_after_manifest')


class Rejected(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def fail(code):
    raise Rejected(code)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')


def pairs(items):
    obj = {}
    for k, v in items:
        if k in obj:
            fail('duplicate_json_key')
        obj[k] = v
    return obj


def json_read(raw):
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: fail('nonfinite_json'))


def b64(data):
    return base64.b64encode(data).decode('ascii')


def unb64(text, maximum=MAX_EXPORT):
    if not isinstance(text, str) or len(text) > ((maximum + 2) // 3) * 4:
        fail('blob_limit')
    try:
        out = base64.b64decode(text, validate=True)
    except (ValueError, UnicodeError):
        fail('invalid_base64')
    if len(out) > maximum or b64(out) != text:
        fail('invalid_base64')
    return out


def records(data):
    try:
        text = data.decode('utf-8')
    except UnicodeError:
        fail('invalid_utf8')
    position, answer = 0, []
    while position < len(text):
        start, cells = position, []
        while True:
            match = FIELD.match(text, position)
            if match is None:
                fail('invalid_csv')
            quoted, bare, separator = match.groups()
            cell = quoted.replace('""', '"') if quoted is not None else None if bare == '\\N' else bare
            cells.append(cell)
            position = match.end()
            if separator == ',':
                continue
            answer.append({'raw': text[start:position].encode('utf-8'), 'cells': cells})
            break
    return answer


def schema_check(raw):
    s = json_read(raw)
    if (not isinstance(s, dict) or set(s) != {'version', 'dialect', 'tables'}
            or type(s['version']) is not int or s['version'] != 1 or s['dialect'] != 'csv-lexical-v1'
            or not isinstance(s['tables'], list) or len(s['tables']) != 4):
        fail('unsupported_schema')
    edges = {'titles': set(), 'entities': set(), 'episodes': {('id', 'titles'), ('series_id', 'titles')},
             'relationships': {('title_id', 'titles'), ('entity_id', 'entities')}}
    for table, name in zip(s['tables'], TABLES):
        if (not isinstance(table, dict) or set(table) - {'name', 'columns', 'primary_key', 'foreign_keys'}
                or table.get('name') != name or table.get('primary_key') != ['id']
                or not isinstance(table.get('columns'), list)):
            fail('unsupported_schema')
        columns = table['columns']
        if len(columns) != len(COLUMNS[name]):
            fail('unsupported_schema')
        for column, expected in zip(columns, COLUMNS[name]):
            if (not isinstance(column, dict) or set(column) - {'name', 'type', 'nullable', 'derived'}
                    or (column.get('name'), column.get('type'), column.get('nullable')) != expected
                    or type(column.get('nullable')) is not bool):
                fail('unsupported_schema')
            derived = column.get('derived')
            wanted = None
            if name == 'titles' and expected[0] == 'external_id':
                wanted = {'kind': 'prefix_id', 'source': 'id', 'prefix': 'tt', 'width': 7}
            if name == 'titles' and expected[0] == 'original_title':
                wanted = {'kind': 'copy', 'source': 'title'}
            if derived != wanted:
                fail('unsupported_schema')
        found = set()
        for edge in table.get('foreign_keys', []):
            if (not isinstance(edge, dict) or set(edge) != {'columns', 'table', 'references'}
                    or not isinstance(edge['columns'], list) or len(edge['columns']) != 1
                    or edge['references'] != ['id']):
                fail('unsupported_schema')
            found.add((edge['columns'][0], edge['table']))
        if found != edges[name]:
            fail('unsupported_schema')
    return s


def checked(tables):
    all_rows, indexes = {}, {}
    for name in TABLES:
        rows = records(tables[name])
        idx = {}
        for row in rows:
            if len(row['cells']) != len(COLUMNS[name]):
                fail('column_count')
            values = []
            for cell, (_, kind, nullable) in zip(row['cells'], COLUMNS[name]):
                if cell is None:
                    if not nullable:
                        fail('null_constraint')
                    value = None
                elif kind == 'text':
                    value = cell
                else:
                    if len(cell) > 4096 or INTEGER.fullmatch(cell) is None:
                        fail('integer_spelling')
                    value = int(cell)
                    low, high = (0, (1 << 64) - 1) if kind == 'uint64' else (-(1 << 63), (1 << 63) - 1)
                    if not low <= value <= high:
                        fail('integer_range')
                values.append(value)
            row['values'] = values
            if values[0] in idx:
                fail('duplicate_primary_key')
            idx[values[0]] = row
            if name == 'titles' and (values[1] != 'tt' + str(values[0]).zfill(7) or values[2] != values[4]):
                fail('derived_field_mismatch')
        all_rows[name], indexes[name] = rows, idx
    for row in all_rows['episodes']:
        if row['values'][0] not in indexes['titles'] or row['values'][1] not in indexes['titles']:
            fail('foreign_key')
    for row in all_rows['relationships']:
        if row['values'][1] not in indexes['titles'] or row['values'][2] not in indexes['entities']:
            fail('foreign_key')
    return all_rows, indexes


def bundle_read(raw):
    if not 44 <= len(raw) <= MAX_EXPORT or raw[:4] != b'RLB1' or hashlib.sha256(raw[:-32]).digest() != raw[-32:]:
        fail('invalid_bundle')
    cursor, end = 4, len(raw) - 32

    def take(size):
        nonlocal cursor
        if size < 0 or cursor + size > end:
            fail('truncated_bundle')
        result = raw[cursor:cursor + size]
        cursor += size
        return result

    length = struct.unpack('<I', take(4))[0]
    if length > 1024 * 1024:
        fail('schema_limit')
    schema = take(length)
    schema_check(schema)
    if struct.unpack('<I', take(4))[0] != 4:
        fail('table_count')
    tables = {}
    for expected in TABLES:
        length = struct.unpack('<H', take(2))[0]
        if length > 1024 or take(length).decode('utf-8') != expected:
            fail('table_name_or_order')
        tables[expected] = take(struct.unpack('<Q', take(8))[0])
    if cursor != end:
        fail('trailing_bundle_bytes')
    checked(tables)
    return schema, tables


def bundle_write(schema, tables):
    chunks = [b'RLB1', struct.pack('<I', len(schema)), schema, struct.pack('<I', 4)]
    for name in TABLES:
        key, data = name.encode('utf-8'), tables[name]
        # MUTATION_EXPORT
        chunks.extend((struct.pack('<H', len(key)), key, struct.pack('<Q', len(data)), data))
    body = b''.join(chunks)
    return body + hashlib.sha256(body).digest()


def query_write(rows):
    size = 8
    for table, data in rows:
        size += 10 + len(table.encode('utf-8')) + len(data)
        if size > MAX_EXPORT:
            fail('query_output_limit')
    pieces = [struct.pack('<4sI', b'QRY1', len(rows))]
    for table, data in rows:
        name = table.encode('utf-8')
        pieces.append(struct.pack('<H', len(name)) + name + struct.pack('<Q', len(data)) + data)
    return b''.join(pieces)


def read_file(name, limit=MAX_STATE):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > limit:
            fail('unsafe_store_file')
        parts, total = [], 0
        while True:
            part = os.read(fd, min(65536, limit - total + 1))
            if not part:
                break
            total += len(part)
            if total > limit:
                fail('store_file_limit')
            parts.append(part)
        return b''.join(parts)
    finally:
        os.close(fd)


def write_all(fd, data):
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise OSError(errno.EIO, 'zero-byte write')
        offset += written


def sync_directory():
    fd = os.open('.', os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Store:
    def __init__(self):
        self.opened = self.poisoned = False
        self.lock_fd = self.wal_fd = None
        self.fault = None
        self.head = self.generation = self.checkpoint = self.user_insert_bytes = 0
        self.schema, self.tables, self.receipts = b'', {}, {}
        self.rows, self.indexes = {}, {}
        self.tail_hash = bytes(32)

    def metadata(self):
        return {'head': self.head, 'generation': self.generation, 'checkpoint': self.checkpoint}

    def acquire(self):
        if self.lock_fd is not None:
            fail('already_open')
        fd = os.open('LOCK', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                fail('unsafe_store_lock')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            fail('single_writer_busy')
        except BaseException:
            os.close(fd)
            raise
        self.lock_fd = fd

    def close(self):
        if self.wal_fd is not None:
            os.close(self.wal_fd)
            self.wal_fd = None
        if self.lock_fd is not None:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            os.close(self.lock_fd)
            self.lock_fd = None
        self.opened = False
        return self.metadata()

    def trip(self, point):
        if self.fault is None or self.fault['point'] != point:
            return
        fault, self.fault = self.fault, None
        if fault['mode'] == 'error':
            raise OSError(errno.EIO, 'injected ' + point)
        fd = os.open('fault.ready', os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            write_all(fd, encoded({'point': point, **self.metadata()}))
            os.fsync(fd)
        finally:
            os.close(fd)
        sync_directory()
        while True:
            time.sleep(3600)  # The evaluator sends a real external SIGKILL.

    def put_new(self, name, data, fsync_fault=None):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            write_all(fd, data)
            if fsync_fault:
                self.trip(fsync_fault)
            os.fsync(fd)
        finally:
            os.close(fd)

    def names(self, generation=None):
        g = self.generation if generation is None else generation
        return 'data-%020d.json' % g, 'wal-%020d.log' % g

    def remove_orphans(self):
        # MUTATION_ORPHANS
        live = set(self.names()) | {'CURRENT', 'LOCK'}
        for name in os.listdir('.'):
            if name in live:
                continue
            known = (re.fullmatch(r'(?:data-[0-9]{20}\.json|wal-[0-9]{20}\.log)', name)
                     or name.endswith('.tmp') or name in ('fault.ready', 'fault.release'))
            if not known:
                fail('unknown_store_file')
            st = os.lstat(name)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                fail('unsafe_orphan')
            os.unlink(name)
        sync_directory()

    def checkpoint_to(self, generation):
        if not 0 <= generation < (1 << 64):
            fail('generation_exhausted')
        snapshot = {'version': 1, 'generation': generation, 'head': self.head,
                    'schema_b64': b64(self.schema), 'tables': [[n, b64(self.tables[n])] for n in TABLES],
                    'receipts': self.receipts, 'user_insert_bytes': self.user_insert_bytes}
        raw = encoded(snapshot)
        if len(raw) > MAX_STATE:
            fail('store_limit')
        checksum = hashlib.sha256(raw).digest()
        data_name, wal_name = self.names(generation)
        self.put_new(data_name, raw, 'merge_snapshot_fsync')
        self.put_new(wal_name, struct.pack('<4sQQ', b'WAL1', generation, self.head) + checksum)
        sync_directory()
        self.trip('merge_before_manifest')
        current = {'version': 1, 'generation': generation, 'checkpoint': self.head,
                   'snapshot': data_name, 'sha256': checksum.hex(), 'wal': wal_name}
        self.put_new('CURRENT.tmp', encoded(current))
        self.trip('manifest_rename')
        os.replace('CURRENT.tmp', 'CURRENT')
        self.trip('directory_fsync')
        sync_directory()
        new_fd = os.open(wal_name, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        if self.wal_fd is not None:
            os.close(self.wal_fd)
        self.wal_fd = new_fd
        self.generation, self.checkpoint, self.tail_hash = generation, self.head, checksum
        self.trip('merge_after_manifest')
        self.remove_orphans()
        return self.metadata()

    def build(self, args):
        if self.opened or self.lock_fd is not None:
            fail('already_open')
        if set(args) != {'bundle_b64'}:
            fail('invalid_build')
        schema, tables = bundle_read(unb64(args['bundle_b64']))
        self.acquire()
        if set(os.listdir('.')) - {'LOCK'}:
            fail('store_not_empty')
        self.schema, self.tables, self.receipts = schema, tables, {}
        self.rows, self.indexes = checked(tables)
        self.head = self.generation = self.checkpoint = self.user_insert_bytes = 0
        self.checkpoint_to(0)
        self.opened, self.poisoned = True, False
        return self.metadata()

    def stage(self, args):
        if (set(args) != {'token', 'records'} or not isinstance(args['token'], str)
                or TOKEN.fullmatch(args['token']) is None or not isinstance(args['records'], list)
                or not 1 <= len(args['records']) <= 128):
            fail('invalid_transaction')
        table_data = dict(self.tables)  # MUTATION_ATOMIC_STAGE
        total = 0
        for record in args['records']:
            if (not isinstance(record, dict) or set(record) != {'table', 'record_b64'}
                    or not isinstance(record['table'], str) or record['table'] not in table_data):
                fail('invalid_insert')
            new = unb64(record['record_b64'], 256 * 1024)
            if len(records(new)) != 1:
                fail('one_record_required')
            total += len(new)
            if total > 512 * 1024:
                fail('transaction_limit')
            old = table_data[record['table']]
            separator = b'\n' if old and old[-1:] not in (b'\r', b'\n') else b''
            table_data[record['table']] = old + separator + new
        rows, indexes = checked(table_data)
        if len(bundle_write(self.schema, table_data)) > MAX_EXPORT:
            fail('store_limit')
        return table_data, rows, indexes, total

    def transaction(self, args):
        # Identity and existing receipts are checked before duplicate-key checks.
        if (set(args) != {'token', 'records'} or not isinstance(args.get('token'), str)
                or TOKEN.fullmatch(args['token']) is None):
            fail('invalid_transaction')
        identity = hashlib.sha256(encoded(args)).hexdigest()
        if args['token'] in self.receipts:
            receipt = self.receipts[args['token']]
            if receipt['digest'] != identity:
                fail('token_conflict')
            return {**self.metadata(), 'txid': receipt['txid'], 'replayed': True}
        if self.head >= (1 << 64) - 1 or len(self.receipts) >= 10000:
            fail('transaction_id_exhausted')
        tables, rows, indexes, logical = self.stage(args)
        txid = self.head + 1
        entry = {'txid': txid, 'token': args['token'], 'digest': identity, 'records': args['records']}
        payload = encoded(entry)
        if len(payload) > MAX_REQUEST:
            fail('transaction_limit')
        checksum = hashlib.sha256(self.tail_hash + payload).digest()
        frame = struct.pack('<I', len(payload)) + payload + checksum
        if self.fault is not None and self.fault['point'] == 'wal_short_write':
            cut = max(1, len(frame) // 2)
            write_all(self.wal_fd, frame[:cut])
            self.trip('wal_short_write')
            write_all(self.wal_fd, frame[cut:])
        else:
            write_all(self.wal_fd, frame)
        self.trip('wal_fsync')
        os.fsync(self.wal_fd)
        self.trip('wal_after_fsync')
        self.tables, self.rows, self.indexes = tables, rows, indexes
        self.head, self.tail_hash = txid, checksum
        self.user_insert_bytes += logical
        self.receipts[args['token']] = {'txid': txid, 'digest': identity}
        return {**self.metadata(), 'txid': txid, 'replayed': False}

    def open(self, args):
        if args:
            fail('invalid_open')
        if self.opened or self.lock_fd is not None:
            fail('already_open')
        self.acquire()
        current = json_read(read_file('CURRENT', 65536))
        if (not isinstance(current, dict) or set(current) != {'version', 'generation', 'checkpoint', 'snapshot', 'sha256', 'wal'}
                or current['version'] != 1 or type(current['generation']) is not int or not 0 <= current['generation'] < (1 << 64)
                or type(current['checkpoint']) is not int or not 0 <= current['checkpoint'] < (1 << 64)):
            fail('corrupt_current')
        data_name, wal_name = self.names(current['generation'])
        if current['snapshot'] != data_name or current['wal'] != wal_name:
            fail('corrupt_current')
        raw = read_file(data_name)
        if hashlib.sha256(raw).hexdigest() != current['sha256']:
            fail('snapshot_checksum')
        saved = json_read(raw)
        if (not isinstance(saved, dict) or set(saved) != {'version', 'generation', 'head', 'schema_b64', 'tables', 'receipts', 'user_insert_bytes'}
                or saved['version'] != 1 or saved['head'] != current['checkpoint']
                or saved['generation'] != current['generation'] or not isinstance(saved['tables'], list)
                or len(saved['tables']) != 4 or not isinstance(saved['receipts'], dict)):
            fail('corrupt_snapshot')
        self.schema = unb64(saved['schema_b64'], 1024 * 1024)
        schema_check(self.schema)
        tables = {}
        for pair, name in zip(saved['tables'], TABLES):
            if not isinstance(pair, list) or len(pair) != 2 or pair[0] != name:
                fail('corrupt_snapshot')
            tables[name] = unb64(pair[1])
        self.rows, self.indexes = checked(tables)
        self.tables, self.receipts = tables, saved['receipts']
        self.head, self.generation, self.checkpoint = saved['head'], saved['generation'], saved['head']
        self.user_insert_bytes = saved['user_insert_bytes']
        if type(self.user_insert_bytes) is not int or self.user_insert_bytes < 0 or len(self.receipts) != self.head:
            fail('corrupt_receipts')
        ids = set()
        for token, receipt in self.receipts.items():
            if (TOKEN.fullmatch(token) is None or not isinstance(receipt, dict) or set(receipt) != {'txid', 'digest'}
                    or type(receipt['txid']) is not int or not 1 <= receipt['txid'] <= self.head
                    or not isinstance(receipt['digest'], str) or re.fullmatch(r'[0-9a-f]{64}', receipt['digest']) is None):
                fail('corrupt_receipts')
            ids.add(receipt['txid'])
        if len(ids) != self.head:
            fail('corrupt_receipts')
        wal = read_file(wal_name)
        header = struct.pack('<4sQQ', b'WAL1', self.generation, self.head) + bytes.fromhex(current['sha256'])
        if not wal.startswith(header):
            fail('stale_wal_generation')
        offset = len(header)
        previous = header[-32:]
        while offset < len(wal):
            if len(wal) - offset < 4:
                break
            length = struct.unpack_from('<I', wal, offset)[0]
            if not 1 <= length <= MAX_REQUEST:
                fail('corrupt_wal_length')
            end = offset + 4 + length + 32
            if end > len(wal):
                break
            payload = wal[offset + 4:offset + 4 + length]
            checksum = wal[offset + 4 + length:end]
            if hashlib.sha256(previous + payload).digest() != checksum:
                fail('wal_checksum')
            entry = json_read(payload)
            if (not isinstance(entry, dict) or set(entry) != {'txid', 'token', 'digest', 'records'}
                    or type(entry['txid']) is not int or entry['txid'] != self.head + 1
                    or not isinstance(entry['token'], str) or entry['token'] in self.receipts):
                fail('corrupt_wal_transaction')
            args = {'token': entry['token'], 'records': entry['records']}
            if hashlib.sha256(encoded(args)).hexdigest() != entry['digest']:
                fail('wal_transaction_digest')
            tables, rows, indexes, logical = self.stage(args)
            self.tables, self.rows, self.indexes = tables, rows, indexes
            self.head = entry['txid']
            self.user_insert_bytes += logical
            self.receipts[entry['token']] = {'txid': self.head, 'digest': entry['digest']}
            previous, offset = checksum, end
        # Only an incomplete final record is truncated. A complete bad checksum,
        # wrong generation, invalid transaction or corrupt snapshot is refused.
        self.wal_fd = os.open(wal_name, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW)
        if offset != len(wal):
            os.ftruncate(self.wal_fd, offset)
            os.fsync(self.wal_fd)
        self.tail_hash = previous
        self.remove_orphans()
        # MUTATION_COUNTER
        self.opened, self.poisoned = True, False
        return self.metadata()

    def point(self, args):
        if (set(args) != {'table', 'id'} or not isinstance(args['table'], str) or args['table'] not in TABLES
                or type(args['id']) is not int or not 0 <= args['id'] < (1 << 64)):
            fail('invalid_query')
        found = self.indexes[args['table']].get(args['id'])
        data = query_write([] if found is None else [(args['table'], found['raw'])])
        return {'bytes_b64': b64(data), 'rows': 0 if found is None else 1}

    def join(self, args):
        if (set(args) != {'kind', 'id'} or args['kind'] not in ('title_entities', 'episodes')
                or type(args['id']) is not int or not 0 <= args['id'] < (1 << 64)):
            fail('invalid_query')
        title = self.indexes['titles'].get(args['id'])
        output = []
        if title is not None:
            output.append(('titles', title['raw']))
            if args['kind'] == 'title_entities':
                for relation in self.rows['relationships']:
                    if relation['values'][1] == args['id']:  # MUTATION_JOIN
                        output.append(('relationships', relation['raw']))
                        output.append(('entities', self.indexes['entities'][relation['values'][2]]['raw']))
            else:
                for episode in self.rows['episodes']:
                    if episode['values'][1] == args['id']:
                        output.append(('episodes', episode['raw']))
                        output.append(('titles', self.indexes['titles'][episode['values'][0]]['raw']))
        return {'bytes_b64': b64(query_write(output)), 'rows': len(output)}

    def dispatch(self, operation, args):
        if operation == 'hello':
            if args:
                fail('invalid_hello')
            return {'protocol_version': 1, 'capabilities': ['single_writer', 'insert_only', 'atomic_bundles',
                    'episodes', 'relationships', 'foreign_keys', 'idempotent_tokens', 'point', 'join',
                    'merge', 'canonical_export', 'process_crash_only'],
                    'fault_points': list(FAULTS), 'max_request_bytes': MAX_REQUEST,
                    'max_response_bytes': MAX_RESPONSE, 'power_loss_certified': False}
        if operation == 'close':
            if args:
                fail('invalid_close')
            return self.close()
        if operation in ('update', 'delete', 'concurrent_writer'):
            fail('unsupported_operation')
        if operation == 'build':
            return self.build(args)
        if operation == 'open':
            return self.open(args)
        if operation == 'stats':
            if args:
                fail('invalid_stats')
            return {**self.metadata(), 'opened': self.opened, 'recovery_required': self.poisoned,
                    'receipt_count': len(self.receipts), 'receipt_payload_bytes': len(encoded(self.receipts)),
                    'receipt_storage': 'Embedded in checkpoint snapshot and WAL; not additive to those file bytes',
                    'index_storage': 'In-memory only', 'user_insert_bytes': self.user_insert_bytes}
        if not self.opened:
            fail('not_open')
        if self.poisoned:
            fail('recovery_required')
        if operation == 'fault':
            if set(args) != {'point', 'mode'} or args['point'] not in FAULTS or args['mode'] not in ('pause', 'error'):
                fail('invalid_fault')
            self.fault = dict(args)
            return {'armed': args['point'], 'mode': args['mode']}
        if operation == 'transaction':
            return self.transaction(args)
        if operation in ('merge', 'checkpoint'):
            if args:
                fail('invalid_merge')
            return self.checkpoint_to(self.generation + 1)
        if operation == 'point':
            return self.point(args)
        if operation == 'join':
            return self.join(args)
        if operation == 'export':
            if args:
                fail('invalid_export')
            raw = bundle_write(self.schema, self.tables)
            return {'bytes_b64': b64(raw), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        fail('unsupported_operation')


def main():
    store = Store()
    previous_id = 0
    try:
        while True:
            line = sys.stdin.buffer.readline(MAX_REQUEST + 1)
            if not line:
                return 0
            if len(line) > MAX_REQUEST or not line.endswith(b'\n'):
                return 2
            try:
                req = json_read(line)
                if (not isinstance(req, dict) or set(req) != {'version', 'id', 'op', 'args'}
                        or type(req['version']) is not int or req['version'] != 1
                        or type(req['id']) is not int or not previous_id < req['id'] < (1 << 63)
                        or not isinstance(req['op'], str) or not 1 <= len(req['op']) <= 64
                        or not isinstance(req['args'], dict)):
                    return 2
            except (ValueError, Rejected, RecursionError, TypeError):
                return 2
            previous_id = req['id']
            response = {'version': 1, 'id': req['id']}
            try:
                response.update(ok=True, result=store.dispatch(req['op'], req['args']))
            except OSError as exc:
                store.poisoned = True  # MUTATION_IO_POISON
                response.update(ok=False, error={'code': 'io_error', 'message': str(exc)[:1000], 'recovery_required': True})
            except Rejected as exc:
                response.update(ok=False, error={'code': exc.code, 'recovery_required': store.poisoned})
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
                # Invalid persistence cannot be interpreted as an empty store.
                store.poisoned = True
                response.update(ok=False, error={'code': 'invalid_data', 'message': type(exc).__name__, 'recovery_required': True})
            raw = encoded(response) + b'\n'
            if len(raw) > MAX_RESPONSE:
                return 2
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()
            if req['op'] == 'close' and response['ok']:
                return 0
    finally:
        store.close()


if __name__ == '__main__':
    sys.exit(main())
