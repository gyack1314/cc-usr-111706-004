"""按来源身份原子登记原文、解释记录与原文附件。"""
import base64
import binascii
import hashlib
import json
import sqlite3

from .domain import decode, normalize

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts(
    collection TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    source_url TEXT NOT NULL,
    status INTEGER NOT NULL,
    raw BLOB NOT NULL,
    PRIMARY KEY(collection,body_sha256)
);
CREATE TABLE IF NOT EXISTS entities(
    collection TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version TEXT NOT NULL,
    receipt_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    kind TEXT,
    permit_number TEXT,
    unit_id TEXT,
    equipment_id TEXT,
    fuel TEXT,
    capacity_mw REAL,
    period_start TEXT,
    period_end TEXT,
    phase TEXT,
    record_status TEXT,
    facility_id TEXT,
    PRIMARY KEY(collection,entity_type,entity_id,version),
    FOREIGN KEY(collection,receipt_sha256)
        REFERENCES receipts(collection,body_sha256)
);
CREATE TABLE IF NOT EXISTS attachments(
    collection TEXT NOT NULL,
    receipt_sha256 TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version TEXT NOT NULL,
    name TEXT NOT NULL,
    source_url TEXT,
    content_type TEXT,
    sha256 TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    raw BLOB NOT NULL,
    PRIMARY KEY(collection,receipt_sha256,entity_type,entity_id,version,name,sha256),
    FOREIGN KEY(collection,receipt_sha256)
        REFERENCES receipts(collection,body_sha256)
);
"""
_ENTITY_COLUMNS = ('kind', 'permit_number', 'unit_id', 'equipment_id', 'fuel',
                   'capacity_mw', 'period_start', 'period_end', 'phase',
                   'record_status', 'facility_id')


def connect(path):
    c = sqlite3.connect(path)
    c.execute('PRAGMA foreign_keys=ON')
    c.executescript(SCHEMA)
    _migrate(c)
    c.execute('CREATE INDEX IF NOT EXISTS entities_unit ON entities('
              'collection,unit_id,permit_number,equipment_id)')
    c.commit()
    return c


def _migrate(c):
    """旧库升级时补列；已有记录不删除、不重写。"""
    for table, columns in (('entities', _ENTITY_COLUMNS),):
        present = {row[1] for row in c.execute(f'PRAGMA table_info({table})')}
        for column in columns:
            if column not in present:
                c.execute(f'ALTER TABLE {table} ADD COLUMN {column} TEXT')
    c.commit()


def _inline_attachments(payload):
    raw = payload.get('attachments') or payload.get('documents') or []
    if isinstance(raw, (str, dict)):
        raw = [raw]
    for item in raw:
        if not isinstance(item, dict):
            continue
        content = item.get('content_base64')
        if content is None and item.get('encoding') in ('base64', 'b64'):
            content = item.get('content') or item.get('data')
        if content is None and isinstance(item.get('raw'), (bytes, bytearray)):
            yield item, bytes(item['raw'])
            continue
        if not isinstance(content, str) or not content:
            continue
        try:
            blob = base64.b64decode(''.join(content.split()), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError('附件原文不是有效的 base64') from exc
        yield item, blob


def _store_attachments(c, collection, sha, row, payload):
    for item, blob in _inline_attachments(payload):
        declared = item.get('sha256')
        digest = hashlib.sha256(blob).hexdigest()
        if declared and declared != digest:
            raise ValueError('附件原文与声明摘要不符')
        name = str(item.get('name') or item.get('filename') or 'attachment')
        url = item.get('url') or item.get('href')
        content_type = item.get('content_type') or item.get('media_type')
        c.execute(
            'INSERT OR IGNORE INTO attachments VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (collection, sha, row['type'], row['id'], row['version'], name,
             url, content_type, digest, len(blob), blob))


def _attachment_target(receipt):
    target = receipt.get('attachment_of')
    if not target:
        return None
    if not isinstance(target, dict):
        return None
    entity_type = target.get('type') or target.get('entity_type')
    entity_id = target.get('id') or target.get('entity_id')
    version = target.get('version')
    name = (target.get('name') or target.get('filename') or
            receipt.get('attachment_name') or receipt.get('filename'))
    if not (entity_type and entity_id and version and name):
        raise ValueError('附件关联信息不完整')
    return {'entity_type': entity_type, 'entity_id': entity_id,
            'version': version, 'name': name}


def _declared_target(c, collection, receipt):
    """按声明中的附件地址找回所属实体；同名文件只在唯一时采用。"""
    url = receipt.get('url') or ''
    name = receipt.get('attachment_name') or receipt.get('filename')
    url_name = url.rsplit('/', 1)[-1] if url else ''
    rows = c.execute(
        'SELECT entity_type,entity_id,version,payload_json FROM entities '
        'WHERE collection=? ORDER BY rowid DESC', (collection,)).fetchall()
    exact, loose = [], []
    for entity_type, entity_id, version, payload_json in rows:
        payload = json.loads(payload_json)
        for key in ('attachments', 'attachment', 'documents', 'document',
                    'files', 'source_files'):
            items = payload.get(key)
            if isinstance(items, (str, dict)):
                items = [items]
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_url = item.get('url') or item.get('href')
                item_name = str(item.get('name') or item.get('filename') or '')
                target = {'entity_type': entity_type, 'entity_id': entity_id,
                          'version': version,
                          'name': item_name or name or url_name or 'attachment'}
                if url and item_url == url:
                    if target not in exact:
                        exact.append(target)
                elif ((name and item_name == name) or
                      (url_name and item_url and
                       item_url.rsplit('/', 1)[-1] == url_name)):
                    if target not in loose:
                        loose.append(target)
    matches = exact or loose
    return matches[0] if len(matches) == 1 else None


def _looks_binary(raw, receipt):
    media = (receipt.get('content_type') or receipt.get('media_type') or '').lower()
    if media and 'json' not in media and 'text' not in media:
        return True
    stripped = raw.lstrip(b' \t\r\n')
    if stripped.startswith(b'\xef\xbb\xbf'):
        stripped = stripped[3:].lstrip(b' \t\r\n')
    return stripped[:1] not in (b'{', b'[')


def receive(c, collection, raw, receipt):
    if receipt['status'] != 200:
        raise ValueError('来源未返回成功正文')
    sha = hashlib.sha256(raw).hexdigest()
    if sha != receipt['body_sha256']:
        raise ValueError('收据与原文不符')
    target = _attachment_target(receipt)
    unlinked = False
    if target is None and _looks_binary(raw, receipt):
        declared = _declared_target(c, collection, receipt)
        if declared is None:
            unlinked = True
        else:
            declared['content_type'] = (receipt.get('content_type') or
                                        receipt.get('media_type'))
            target = declared
    if target is not None or unlinked:
        with c:
            c.execute('INSERT OR IGNORE INTO receipts VALUES(?,?,?,?,?)',
                      (collection, sha, receipt['url'], receipt['status'], raw))
            if target is not None:
                linked = c.execute(
                    'SELECT 1 FROM entities WHERE collection=? AND entity_type=? '
                    'AND entity_id=? AND version=?',
                    (collection, target['entity_type'], target['entity_id'],
                     target['version'])).fetchone()
                if linked:
                    c.execute(
                        'INSERT OR IGNORE INTO attachments VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (collection, sha, target['entity_type'],
                         target['entity_id'], target['version'], target['name'],
                         receipt['url'], target.get('content_type') or
                         receipt.get('content_type') or receipt.get('media_type'),
                         sha, len(raw), raw))
        return sha
    rows = decode(raw, receipt)
    with c:
        c.execute('INSERT OR IGNORE INTO receipts VALUES(?,?,?,?,?)',
                  (collection, sha, receipt['url'], receipt['status'], raw))
        for row in rows:
            payload = json.dumps(row['payload'], ensure_ascii=False, sort_keys=True)
            key = (collection, row['type'], row['id'], row['version'])
            prior = c.execute(
                'SELECT receipt_sha256,payload_json FROM entities '
                'WHERE collection=? AND entity_type=? AND entity_id=? AND version=?',
                key).fetchone()
            if prior and prior != (sha, payload):
                raise ValueError('同一实体版本原文冲突')
            fields = normalize(row['payload'])
            period = fields.get('applicable_period') or {}
            c.execute(
                'INSERT OR IGNORE INTO entities VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (*key, sha, payload, fields.get('kind'),
                 fields.get('permit_number'), fields.get('unit_id'),
                 fields.get('equipment_id'), fields.get('fuel'),
                 fields.get('capacity_mw'), period.get('start'),
                 period.get('end'), fields.get('phase'),
                 fields.get('status') or ('withdrawn' if fields.get('kind') == 'withdrawal' else None),
                 fields.get('facility_id')))
            _store_attachments(c, collection, sha, row, row['payload'])
    return sha


def unlinked_receipts(c, collection):
    """已接收但既不是记录、也没有已登记附件归属的原文。"""
    rows = c.execute(
        'SELECT r.body_sha256,r.source_url,r.status,r.raw '
        'FROM receipts r WHERE r.collection=? AND NOT EXISTS('
        '  SELECT 1 FROM entities e WHERE e.collection=r.collection '
        '  AND e.receipt_sha256=r.body_sha256) AND NOT EXISTS('
        '  SELECT 1 FROM attachments a WHERE a.collection=r.collection '
        '  AND a.receipt_sha256=r.body_sha256) ORDER BY r.rowid', (collection,))
    return [dict(source_sha256=row[0], source_url=row[1], status=row[2],
                 bytes=len(row[3]), reason='unlinked') for row in rows]


def entities(c, collection):
    rows = c.execute(
        'SELECT e.entity_type,e.entity_id,e.version,e.receipt_sha256,'
        'e.payload_json,r.source_url,r.rowid,e.rowid,e.kind,e.record_status '
        'FROM entities e JOIN receipts r '
        'ON r.collection=e.collection AND r.body_sha256=e.receipt_sha256 '
        'WHERE e.collection=? ORDER BY r.rowid,e.rowid', (collection,))
    return [dict(type=row[0], id=row[1], version=row[2], source=row[3],
                 payload=json.loads(row[4]), source_url=row[5],
                 sequence=row[6], record_order=row[7], kind=row[8],
                 record_status=row[9]) for row in rows]


def attachments(c, collection, entity_type=None, entity_id=None, version=None,
                include_content=False):
    query = ('SELECT entity_type,entity_id,version,name,source_url,content_type,'
             'sha256,bytes,raw,receipt_sha256 FROM attachments WHERE collection=?')
    args = [collection]
    for column, value in (('entity_type', entity_type), ('entity_id', entity_id),
                          ('version', version)):
        if value is not None:
            query += f' AND {column}=?'
            args.append(value)
    query += ' ORDER BY entity_type,entity_id,version,name,sha256'
    out = []
    for row in c.execute(query, args):
        item = dict(entity_type=row[0], entity_id=row[1], version=row[2],
                    name=row[3], source_url=row[4], content_type=row[5],
                    sha256=row[6], bytes=row[7], receipt_sha256=row[9])
        if include_content:
            item['raw'] = bytes(row[8])
        out.append(item)
    return out
