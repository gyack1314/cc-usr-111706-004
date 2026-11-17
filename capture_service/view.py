"""按领域解释结果展示已接收证据、附件及缺口。"""
from .domain import assess
from .ledger import attachments, entities, unlinked_receipts


def overview(c, collection):
    rows = entities(c, collection)
    result = assess(rows)
    result['collection'] = collection
    stored = attachments(c, collection)
    by_entity = {}
    for item in stored:
        key = (item['entity_type'], item['entity_id'], item['version'])
        by_entity.setdefault(key, []).append(item)
    presented = []
    for row in rows:
        payload = row.get('payload') or {}
        record = {'type': row['type'], 'id': row['id'],
                  'version': row['version'],
                  'kind': row.get('kind'),
                  'status': row.get('record_status'),
                  'source_sha256': row['source'],
                  'source_url': row.get('source_url')}
        for name in ('permit', 'permit_number', 'generator', 'unit',
                     'equipment', 'equipment_id', 'site', 'facility',
                     'fuel', 'mw', 'capacity_mw', 'generation_mwh', 'phase',
                     'period', 'applicable_period', 'status', 'filing',
                     'replaces', 'late', 'late_reason'):
            if name in payload:
                record[name] = payload[name]
        record['attachments'] = by_entity.get(
            (row['type'], row['id'], row['version']), [])
        declared = payload.get('attachments') or payload.get('documents') or []
        if isinstance(declared, (str, dict)):
            declared = [declared]
        record['declared_attachments'] = [
            item for item in declared
            if isinstance(item, dict) and not any(
                item.get(name) for name in
                ('content_base64', 'content', 'data', 'raw'))
        ] if isinstance(declared, list) else []
        presented.append(record)
    result['records'] = presented
    result['unlinked_receipts'] = unlinked_receipts(c, collection)
    if result['unlinked_receipts']:
        result['gaps'].append('有来源原文未关联到记录')
        result['reasons'].append('有来源原文未关联到记录')
        result['publishable'] = False
    return result
