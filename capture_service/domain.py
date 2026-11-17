"""解释州许可、设备清单与运行申报；待核来源不转换成确认结论。"""
import json
import re

# 许可生命周期只承认这四个阶段；撤回是状态，不是阶段。
PHASE_ORDER = {'approved': 0, 'installed': 1, 'trial': 2, 'operational': 3}
_PHASE = {
    'approved': 'approved', 'approve': 'approved', 'permit': 'approved',
    'permitted': 'approved', 'issued': 'approved', 'granted': 'approved',
    'authorized': 'approved', 'authorised': 'approved', 'licenced': 'approved',
    'licensed': 'approved',
    'installed': 'installed', 'installation': 'installed',
    'constructed': 'installed', 'erected': 'installed', 'built': 'installed',
    'trial': 'trial', 'trial_run': 'trial', 'trial_operation': 'trial',
    'testing': 'trial', 'test': 'trial', 'commissioning': 'trial',
    'commissioned': 'trial', 'startup': 'trial', 'start_up': 'trial',
    'operational': 'operational', 'operating': 'operational',
    'operation': 'operational', 'in_service': 'operational',
    'in_operation': 'operational', 'commercial': 'operational',
    'commercial_operation': 'operational', 'running': 'operational',
    '获批': 'approved', '批准': 'approved', '许可': 'approved',
    '安装': 'installed', '建成': 'installed',
    '试运行': 'trial', '调试': 'trial', '测试': 'trial',
    '正式运行': 'operational', '运行': 'operational', '商业运行': 'operational',
}
_WITHDRAW_KEYS = ('withdrawal_of', 'withdraws', 'revokes', 'revoked_permit',
                  'permit_ref', 'permit_number', 'permit')
_WITHDRAWAL_ID_KEYS = ('withdrawal', 'withdrawal_id', 'withdrawal_number',
                       'revocation', 'revocation_id', 'revocation_number')
_WITHDRAWN = {
    'withdrawn', 'withdraw', 'revoked', 'revoke', 'rescinded', 'rescind',
    'cancelled', 'canceled', 'cancel', 'void', 'voided',
    'terminated', 'termination', 'withdrawal',
    '撤回', '撤销', '吊销', '废止', '终止', '取消',
}
# 申请被拒不等于曾经获批后被撤回，单独保留，避免把拒绝写成撤回。
_DENIED = {'denied', 'deny', 'rejected', 'reject', 'refused', '拒绝', '驳回'}
# 暂停是临时状态，不等于撤回；单独保留供展示。
_SUSPENDED = {'suspended', 'suspension', 'paused', '暂停', '中止'}
_UNIT_KEYS = ('generator', 'generator_id', 'unit', 'unit_id', 'genset',
              'genset_id', 'engine', 'engine_id', 'generating_unit')
_EQUIP_KEYS = ('equipment', 'equipment_id', 'device', 'device_id', 'asset',
               'asset_id', 'serial', 'serial_number', 'engine_serial')
_PERMIT_KEYS = ('permit', 'permit_id', 'permit_number', 'permit_no',
                'air_permit', 'registration', 'registration_id')
_FILING_KEYS = ('filing', 'filing_id', 'filing_number', 'report', 'report_id',
                'run', 'run_id', 'operation_id', 'operating_report')
_SITE_KEYS = ('site', 'site_id', 'facility', 'facility_id', 'plant',
              'plant_id', 'location', 'address', 'county')
_FUEL_KEYS = ('fuel', 'fuel_type', 'energy_source', 'primary_fuel')
_MODEL_KEYS = ('model', 'equipment_model', 'unit_model', 'make_model',
               'engine_model', 'model_number')
_REPLACE_KEYS = ('replaces', 'replaces_unit', 'replacement_for', 'supersedes',
                 'superseded_unit', 'replaces_equipment')
_CAPACITY_KEYS = ('capacity_mw', 'mw', 'capacity', 'rated_mw',
                  'rated_capacity_mw', 'nameplate_mw', 'output_mw',
                  'gross_mw', 'electrical_mw', 'max_mw')
_GENERATION_KEYS = ('generation_mwh', 'actual_generation_mwh',
                    'net_generation_mwh', 'gross_generation_mwh',
                    'energy_mwh', 'mwh', 'generated_mwh', 'generation',
                    'generated_energy', 'net_generation', 'output_mwh')
_PERIOD_KEYS = ('applicable_period', 'period', 'valid_period',
                'effective_period', 'coverage_period', 'reporting_period',
                'operating_period')
_START_KEYS = ('start', 'from', 'begin', 'valid_from', 'effective_from',
               'applicable_from', 'period_start', 'coverage_start')
_END_KEYS = ('end', 'to', 'until', 'valid_to', 'effective_to',
             'applicable_to', 'period_end', 'coverage_end')


def _first(d, keys):
    for key in keys:
        if key in d and d[key] not in (None, '', [], {}):
            return d[key]
    return None


def _text(value):
    """身份和联系键只接受标量；对象里的 id/name 可以取出。"""
    if isinstance(value, dict):
        value = _first(value, ('id', 'number', 'name', 'value'))
    if value is None or isinstance(value, bool):
        return None
    value = str(value).strip()
    return value or None


def _items(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _key(value):
    text = _text(value)
    if not text:
        return None
    return re.sub(r'[\s-]+', '_', text.lower())


def _phase(value):
    key = _key(value)
    return _PHASE.get(key) if key else None


def _withdrawn(value):
    key = _key(value)
    return bool(key and key in _WITHDRAWN)


def _status(d):
    for key in ('status', 'permit_status', 'disposition', 'current_status',
                'state'):
        if key in d:
            value = _text(d[key])
            if value and _withdrawn(value):
                return 'withdrawn'
            if value and _key(value) in _SUSPENDED:
                return 'suspended'
            if value and _key(value) in _DENIED:
                return 'denied'
            if not value:
                continue
            if key == 'state' and not _phase(value):
                continue
            return _key(value)
    for key in ('withdrawn', 'revoked', 'rescinded', 'cancelled', 'canceled'):
        if d.get(key) is True:
            return 'withdrawn'
    return None


def _number(value, kind='capacity'):
    """从数字、'15 MW' 或 {'value': 15, 'unit': 'MW'} 取值；不猜发电量。"""
    unit = None
    if isinstance(value, dict):
        unit = _text(_first(value, ('unit', 'units', 'uom')))
        value = _first(value, ('value', 'amount', 'mw', 'mwh', 'quantity'))
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        match = re.search(r'[-+]?\d+(?:\.\d+)?', value.replace(',', ''))
        if not match:
            return None
        number = float(match.group())
        if not unit:
            chars = re.search(r'[\u4e00-\u9fff]+', value)
            letters = re.search(r'[A-Za-z]+', value)
            unit = (chars.group() if chars else
                    letters.group() if letters else None)
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        return None
    if number < 0:
        raise ValueError('来源容量或电量不合法')
    factor = 1.0
    if unit:
        unit = unit.strip().lower().replace('_', '').replace('-', '').replace(' ', '')
        if kind == 'capacity':
            factors = {'w': 0.000001, 'kw': 0.001, 'kwe': 0.001,
                       'mw': 1.0, 'mwe': 1.0, 'mwt': 1.0, 'gw': 1000.0,
                       'mva': 1.0, 'kva': 0.001,
                       'watt': 0.000001, 'kilowatt': 0.001,
                       'megawatt': 1.0, 'gigawatt': 1000.0,
                       '瓦': 0.000001, '千瓦': 0.001, '兆瓦': 1.0}
        else:
            factors = {'wh': 0.000001, 'kwh': 0.001, 'mwh': 1.0,
                       'gwh': 1000.0, 'twh': 1000000.0,
                       'watthour': 0.000001, 'kilowatthour': 0.001,
                       'megawatthour': 1.0, 'gigawatthour': 1000.0,
                       '瓦时': 0.000001, '千瓦时': 0.001, '兆瓦时': 1.0}
        if unit not in factors:
            return None
        factor = factors[unit]
    number *= factor
    return int(number) if number.is_integer() else round(number, 6)


def _period(d, keys=None):
    keys = keys or _PERIOD_KEYS
    raw = _first(d, keys)
    start = end = label = None
    if isinstance(raw, dict):
        start = _text(_first(raw, _START_KEYS))
        end = _text(_first(raw, _END_KEYS))
        label = _text(_first(raw, ('label', 'name', 'text', 'value')))
    elif isinstance(raw, str):
        text = raw.strip()
        label = text or None
        if '/' in text:
            a, b = text.split('/', 1)
            start, end = a.strip() or None, b.strip() or None
        elif '..' in text:
            a, b = text.split('..', 1)
            start, end = a.strip() or None, b.strip() or None
        else:
            start = end = text or None
    elif isinstance(raw, (list, tuple)):
        if len(raw) >= 2:
            start, end = _text(raw[0]), _text(raw[1])
            label = f'{start}/{end}' if start and end else _text(raw[0])
        elif raw:
            start = end = _text(raw[0])
            label = start
    elif raw is not None:
        start = end = _text(raw)
    start = start or _text(_first(d, _START_KEYS))
    end = end or _text(_first(d, _END_KEYS))
    if not start and not end:
        year = _text(_first(d, ('year',)))
        month = _text(_first(d, ('month',)))
        if year and month:
            value = f'{year}-{int(month):02d}' if str(month).isdigit() else f'{year}-{month}'
            label = label or value
            start = end = label
        elif year:
            label = label or year
            start = end = year
        elif _text(_first(d, ('date', 'filed_at', 'effective_date'))):
            label = _text(_first(d, ('date', 'filed_at', 'effective_date')))
            start = end = label
    if not (start or end or label):
        return None
    if not label:
        label = (f'{start}/{end}' if start and end and start != end
                 else start or end)
    return {'start': start, 'end': end, 'label': label}


def _kind(d):
    explicit = _text(_first(d, ('kind', 'record_type', 'document_type',
                                'source_type', 'type')))
    key = _key(explicit)
    known = None
    if key in ('permit', 'permitted', 'approval', 'license', 'licence'):
        known = 'permit'
    elif key in ('equipment', 'inventory', 'equipment_inventory', 'device'):
        known = 'equipment'
    elif key in ('filing', 'operation', 'operating', 'run', 'report',
                 'operating_report', 'generation', 'monthly_report'):
        known = 'filing'
    elif key in ('withdrawal', 'revocation', 'withdrawn', 'rescission'):
        known = 'withdrawal'
    if known:
        return known
    if _first(d, _WITHDRAWAL_ID_KEYS) is not None:
        return 'withdrawal'
    if _withdrawn(_first(d, ('phase', 'stage', 'lifecycle', 'lifecycle_phase'))):
        return 'withdrawal'
    if _first(d, _FILING_KEYS) is not None or _first(d, ('filing', 'report', 'run')) is not None:
        return 'filing'
    if _first(d, _EQUIP_KEYS) is not None or _first(d, ('equipment', 'inventory', 'equipment_inventory')) is not None:
        return 'equipment'
    if _first(d, _PERMIT_KEYS) is not None:
        return 'permit'
    return 'permit'


def _type_for(kind):
    # 保留基线类型名：许可为主要来源，运行申报为关联来源。
    return {'permit': 'generator', 'filing': 'support',
            'equipment': 'equipment', 'withdrawal': 'withdrawal'}[kind]


def _identity(kind, d):
    if kind == 'equipment':
        keys = _EQUIP_KEYS + _UNIT_KEYS + _PERMIT_KEYS
    elif kind == 'filing':
        keys = _FILING_KEYS + _UNIT_KEYS + _PERMIT_KEYS
    elif kind == 'withdrawal':
        keys = _WITHDRAWAL_ID_KEYS + _PERMIT_KEYS + _UNIT_KEYS + _FILING_KEYS
    else:
        # 许可按许可编号登记；机组身份另存并用于关联，避免同一机组多份许可互相覆盖。
        keys = _PERMIT_KEYS + _UNIT_KEYS + _EQUIP_KEYS
    return _text(_first(d, keys))


def _attachments(d):
    raw = _first(d, ('attachments', 'attachment', 'documents', 'document',
                     'files', 'source_files', 'attachments_list'))
    out = []
    for item in _items(raw):
        if isinstance(item, dict):
            row = {}
            for name in ('name', 'filename', 'title', 'url', 'href',
                         'sha256', 'content_type', 'media_type', 'bytes'):
                value = _text(item.get(name))
                if value:
                    row['url' if name == 'href' else name] = value
            if row:
                out.append(row)
        else:
            value = _text(item)
            if value:
                out.append({'url': value} if '://' in value else {'name': value})
    return out


def normalize(payload):
    """把一份原文解释成可比较的记录；缺项保持缺项。"""
    if not isinstance(payload, dict):
        raise ValueError('来源正文不是对象')
    kind = _kind(payload)
    phase = _phase(_first(payload, ('phase', 'stage', 'lifecycle',
                                    'lifecycle_phase')))
    status = _status(payload)
    if phase is None and kind in ('permit', 'withdrawal') and status not in (
            None, 'withdrawn', 'denied'):
        phase = _phase(status)
        if phase is None and status in ('active', 'valid', 'effective'):
            phase = 'approved'
    if (kind == 'permit' and phase is None and status is None and
            not _first(payload, ('phase', 'stage', 'lifecycle', 'lifecycle_phase'))):
        phase = 'approved'
    unit_id = _text(_first(payload, _UNIT_KEYS))
    equipment_id = _text(_first(payload, _EQUIP_KEYS))
    permit_number = _text(_first(
        payload, _WITHDRAW_KEYS if kind == 'withdrawal' else _PERMIT_KEYS))
    filing_id = _text(_first(payload, _FILING_KEYS))
    facility = _text(_first(payload, _SITE_KEYS))
    candidates = []
    for key in ('facility_candidates', 'site_candidates', 'candidate_sites',
                'candidate_facilities'):
        for item in _items(payload.get(key)):
            value = _text(item)
            if value and value not in candidates:
                candidates.append(value)
    capacity = _number(_first(payload, _CAPACITY_KEYS), 'capacity')
    generation = _number(_first(payload, _GENERATION_KEYS), 'generation')
    replaces = []
    for key in _REPLACE_KEYS:
        for item in _items(payload.get(key)):
            value = _text(item)
            if value and value not in replaces:
                replaces.append(value)
    return {
        'kind': kind,
        'phase': phase,
        'phase_raw': _text(_first(payload, ('phase', 'stage', 'lifecycle',
                                            'lifecycle_phase'))),
        'status': status,
        'withdrawn': kind == 'withdrawal' or status == 'withdrawn',
        'unit_id': unit_id,
        'equipment_id': equipment_id,
        'permit_number': permit_number,
        'filing_id': filing_id,
        'model': _text(_first(payload, _MODEL_KEYS)),
        'fuel': _text(_first(payload, _FUEL_KEYS)),
        'capacity_mw': capacity,
        'capacity_basis': ('permit' if kind in ('permit', 'withdrawal')
                           else 'nameplate' if kind == 'equipment' else None),
        'generation_mwh': generation,
        'applicable_period': _period(payload),
        'facility_id': facility,
        'facility_candidates': candidates,
        'replaces': replaces,
        'declared_attachments': _attachments(payload),
        'late': bool(payload.get('late') or payload.get('late_arrival')),
        'late_reason': (_text(payload.get('late_reason')) or
                        _text(payload.get('late_arrival_reason'))),
    }


def _documents(d):
    if isinstance(d, list):
        return d, {}
    if not isinstance(d, dict):
        raise ValueError('来源正文不是对象')
    for key in ('records', 'items', 'entries', 'results', 'data'):
        if isinstance(d.get(key), list):
            root = {k: v for k, v in d.items()
                    if k in ('revision', 'rev', 'version', 'source',
                             'reported_period', 'reporting_period',
                             'period', 'applicable_period', 'site',
                             'facility', 'facility_id', 'fuel', 'fuel_type')}
            return d[key], root
    return [d], {}


def decode(raw, receipt):
    try:
        d = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError('来源正文不是有效的 UTF-8 JSON') from exc
    docs, root = _documents(d)
    if not docs:
        raise ValueError('来源没有记录')
    rows = []
    seen = {}
    for item in docs:
        if not isinstance(item, dict) or not item:
            raise ValueError('来源记录格式不明')
        doc = dict(root)
        doc.update(item)
        revision = _text(_first(doc, ('revision', 'rev', 'version')))
        if not revision:
            raise ValueError('来源版次不明')
        kind = _kind(doc)
        identity = _identity(kind, doc)
        if not identity:
            raise ValueError('来源身份不明')
        row = {'type': _type_for(kind), 'id': identity,
               'version': revision, 'kind': kind, 'payload': doc}
        payload = json.dumps(doc, ensure_ascii=False, sort_keys=True)
        key = (row['type'], identity, revision)
        if key in seen and seen[key] != payload:
            raise ValueError('同一响应内同一记录版本内容冲突')
        seen[key] = payload
        rows.append(row)
    return rows


def _ref(rec):
    return {'type': rec['type'], 'id': rec['id'], 'version': rec['version'],
            'source_sha256': rec.get('source_sha256'),
            'received_order': rec.get('received_order')}


def _record_key(rec):
    return f"{rec['type']}:{rec['id']}:{rec['version']}"


def _primary_key(rec):
    if rec.get('unit_id'):
        return 'unit:' + rec['unit_id']
    if rec.get('equipment_id'):
        return 'equipment:' + rec['equipment_id']
    if rec.get('permit_number'):
        return 'permit:' + rec['permit_number']
    return 'record:' + _record_key(rec)


def _group(recs):
    """按机组聚合；设备跟随机组，许可只在唯一指向机组时连接。

    同一许可关联多个机组时保持分组独立，由缺口说明，不做强制归并。
    """
    parent = {}

    def find(key):
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    permit_units = {}
    permit_equipment = {}
    equipment_owner = {}
    for rec in recs:
        find(_primary_key(rec))
        if rec.get('equipment_id') and rec.get('unit_id'):
            equipment_owner[rec['equipment_id']] = rec['unit_id']
        if rec.get('unit_id') and rec.get('equipment_id'):
            union('unit:' + rec['unit_id'], 'equipment:' + rec['equipment_id'])
        if rec.get('permit_number'):
            if rec.get('unit_id'):
                bucket = permit_units.setdefault(rec['permit_number'], [])
                if rec['unit_id'] not in bucket:
                    bucket.append(rec['unit_id'])
            elif rec.get('equipment_id'):
                bucket = permit_equipment.setdefault(rec['permit_number'], [])
                if rec['equipment_id'] not in bucket:
                    bucket.append(rec['equipment_id'])
    for permit, unit_ids in permit_units.items():
        if len(unit_ids) == 1:
            union('permit:' + permit, 'unit:' + unit_ids[0])
    for permit, equipment_ids in permit_equipment.items():
        if len(equipment_ids) == 1:
            union('permit:' + permit, 'equipment:' + equipment_ids[0])
    for rec in recs:
        if rec['kind'] != 'equipment' or rec.get('unit_id'):
            continue
        for old_id in rec.get('replaces') or []:
            owner = equipment_owner.get(old_id)
            if owner:
                union('equipment:' + rec['equipment_id'],
                      'unit:' + owner)
    groups = {}
    for rec in recs:
        root = find(_primary_key(rec))
        groups.setdefault(root, []).append(rec)
    return list(groups.values())


def _is_withdrawn(rec):
    return (rec['kind'] == 'withdrawal' or rec.get('withdrawn') or
            rec.get('status') == 'withdrawn')


def _sort_key(rec):
    order = rec.get('received_order')
    return (order if isinstance(order, int) else 10 ** 9,
            str(rec.get('version') or ''))


def _period_key(rec):
    period = rec.get('applicable_period') or {}
    start = period.get('start') or period.get('end') or ''
    return (str(start), _sort_key(rec))


def _permit_groups(recs):
    groups = {}
    for rec in recs:
        if rec['kind'] not in ('permit', 'withdrawal'):
            continue
        number = rec.get('permit_number') or rec.get('id')
        groups.setdefault(number, []).append(rec)
    return groups


def _facility(recs, gaps):
    attest = {}
    for rec in recs:
        facility = rec.get('facility_id')
        if facility:
            attest.setdefault(facility, []).append(rec)
        for item in rec.get('facility_candidates') or []:
            if item:
                attest.setdefault(item, []).append(rec)
    candidates = list(attest)
    explicit = []
    for rec in recs:
        for item in rec.get('facility_candidates') or []:
            if item and item not in explicit:
                explicit.append(item)
    candidates = [c for c in candidates if c not in explicit] + explicit
    evidence = []
    for facility in candidates:
        seen = set()
        sources = []
        for rec in attest.get(facility, []):
            key = rec.get('source_sha256') or _record_key(rec)
            if key in seen:
                continue
            seen.add(key)
            sources.append(_ref(rec))
        evidence.append({'facility_id': facility, 'sources': sources})
    if not candidates:
        gaps.append('设施归属不明：缺少设施标识')
        return {'status': 'unresolved', 'facility_id': None,
                'candidates': [], 'candidate_ids': [], 'evidence': [],
                'reasons': ['missing_facility']}
    if len(candidates) > 1:
        gaps.append('设施归属存在多个候选，未强制归并')
        return {'status': 'ambiguous', 'facility_id': None,
                'candidates': candidates, 'candidate_ids': candidates,
                'evidence': evidence, 'reasons': ['multiple_candidates']}
    facility = candidates[0]
    if len(evidence[0]['sources']) < 2:
        gaps.append('设施归属仅有一个来源，保留候选')
        return {'status': 'candidate', 'facility_id': None,
                'candidates': candidates, 'candidate_ids': candidates,
                'evidence': evidence, 'reasons': ['single_source']}
    return {'status': 'resolved', 'facility_id': facility,
            'candidates': candidates, 'candidate_ids': candidates,
            'evidence': evidence, 'reasons': []}


def _equipment(recs):
    """保留全部设备记录；换型只追加后继关系，不删除旧设备。"""
    items = [rec for rec in recs if rec['kind'] == 'equipment']
    items.sort(key=_period_key)
    by_id = {}
    order = []
    for rec in items:
        eid = rec.get('equipment_id') or rec.get('unit_id') or rec['id']
        if eid not in by_id:
            by_id[eid] = []
            order.append(eid)
        by_id[eid].append(rec)
    history = []
    active = {}
    for eid in order:
        group = by_id[eid]
        for index, rec in enumerate(group):
            state = 'active' if index == len(group) - 1 else 'superseded'
            superseded_by = None
            change = None
            newer = group[index + 1] if index < len(group) - 1 else None
            if newer is not None:
                superseded_by = {'type': newer['type'], 'id': newer['id'],
                                 'version': newer['version'],
                                 'source_sha256': newer.get('source_sha256')}
                same = all(rec.get(key) == newer.get(key) for key in (
                    'model', 'fuel', 'capacity_mw'))
                change = ('unchanged_revision' if same else
                          'model_changed' if rec.get('model') and
                          newer.get('model') and rec['model'] != newer['model']
                          else 'revised')
            item = {
                'equipment_id': eid,
                'unit_id': rec.get('unit_id'),
                'model': rec.get('model'),
                'fuel': rec.get('fuel'),
                'capacity_mw': rec.get('capacity_mw'),
                'phase': rec.get('phase'),
                'status': state,
                'declared_status': rec.get('status'),
                'change': change,
                'capacity_change_mw': (
                    newer.get('capacity_mw') - rec['capacity_mw']
                    if newer is not None and rec.get('capacity_mw') is not None and
                    newer.get('capacity_mw') is not None else None),
                'superseded_by': superseded_by,
                'replaces': rec.get('replaces') or [],
                'applicable_period': rec.get('applicable_period'),
                'source_sha256': rec.get('source_sha256'),
                'version': rec.get('version'),
            }
            history.append(item)
            if state == 'active':
                active[eid] = item
    # 后一条记录声明替换某台旧设备时，旧设备标记为被替换但保留。
    for rec in items:
        for old_id in rec.get('replaces') or []:
            target = active.get(old_id)
            if not target:
                continue
            target['status'] = 'superseded'
            target['change'] = 'replaced'
            target['superseded_by'] = {
                'type': rec['type'], 'id': rec['id'],
                'version': rec['version'],
                'source_sha256': rec.get('source_sha256')}
            if (target.get('capacity_mw') is not None and
                    rec.get('capacity_mw') is not None):
                target['capacity_change_mw'] = (
                    rec['capacity_mw'] - target['capacity_mw'])
    return history


def _unique_by_period(records):
    """同一机组同一期间只计一次电量，取最新收到的申报。

    全部原始申报仍保留在 generation_records 中，供核对差异。
    """
    chosen = {}
    for rec in records:
        period = (rec.get('period') or {}).get('label')
        key = period or f"{rec['type']}:{rec['id']}:{rec['version']}"
        prior = chosen.get(key)
        if prior is None or _sort_key(rec) >= _sort_key(prior):
            chosen[key] = rec
    return list(chosen.values())


def _generation(recs):
    records = []
    for rec in recs:
        if rec.get('generation_mwh') is None:
            continue
        records.append({'mwh': rec['generation_mwh'], 'phase': rec.get('phase'),
                        'period': rec.get('applicable_period'),
                        'type': rec['type'], 'id': rec['id'],
                        'version': rec['version'],
                        'received_order': rec.get('received_order'),
                        'source_sha256': rec.get('source_sha256')})
    operational = _unique_by_period(
        [r for r in records if r['phase'] == 'operational'])
    trial = _unique_by_period([r for r in records if r['phase'] == 'trial'])
    return records, operational, trial


def _late(recs):
    """只标注运行申报的到达顺序与适用期间倒挂，不回写、不删除任何记录。"""
    flagged = set()
    filings = [rec for rec in recs
               if rec['kind'] == 'filing' and isinstance(
                   rec.get('received_order'), int)]
    for rec in recs:
        if rec.get('late'):
            flagged.add(_record_key(rec))
            continue
        if rec['kind'] != 'filing':
            continue
        period = rec.get('applicable_period') or {}
        end = period.get('end')
        order = rec.get('received_order')
        if not end or not isinstance(order, int):
            continue
        for other in filings:
            if other is rec or other['received_order'] >= order:
                continue
            prior = other.get('applicable_period') or {}
            start = prior.get('start')
            if start and str(start) > str(end):
                flagged.add(_record_key(rec))
                break
    return flagged


def _unit(recs, gaps):
    recs.sort(key=_sort_key)
    unit_ids = []
    equipment_ids = []
    for rec in recs:
        if rec.get('unit_id') and rec['unit_id'] not in unit_ids:
            unit_ids.append(rec['unit_id'])
        if rec.get('equipment_id') and rec['equipment_id'] not in equipment_ids:
            equipment_ids.append(rec['equipment_id'])
    permit_numbers = []
    for rec in recs:
        if rec.get('permit_number') and rec['permit_number'] not in permit_numbers:
            permit_numbers.append(rec['permit_number'])
    unit_id = (unit_ids[0] if unit_ids else
               permit_numbers[0] if permit_numbers else
               next((rec.get('filing_id') for rec in recs if rec.get('filing_id')), None) or
               recs[0]['id'])

    fuels = []
    for rec in recs:
        fuel = rec.get('fuel')
        if fuel and fuel not in fuels:
            fuels.append(fuel)
    if len(fuels) > 1:
        gaps.append('燃料存在多个候选')
    fuel = fuels[0] if len(fuels) == 1 else None

    permit_unit_ids = {}
    for rec in recs:
        if rec.get('permit_number') and rec.get('unit_id'):
            permit_unit_ids.setdefault(rec['permit_number'], set()).add(rec['unit_id'])
    for number, ids in permit_unit_ids.items():
        if len(ids) > 1:
            gaps.append(f'许可 {number} 关联多个机组，未强制归并')
    if len(permit_unit_ids) > 1:
        gaps.append('一个机组关联多个许可，按各自期间保留')

    permits = []
    for number, group in _permit_groups(recs).items():
        group.sort(key=_sort_key)
        records_only = [rec for rec in group if rec['kind'] == 'permit']
        withdrawal = next((rec for rec in reversed(group)
                           if rec['kind'] == 'withdrawal'), None)
        latest = records_only[-1] if records_only else group[-1]
        withdrawn = _is_withdrawn(latest) or withdrawal is not None
        status = 'withdrawn' if withdrawn else (
            latest.get('status') or latest.get('phase') or 'unknown')
        expired = status in ('expired', 'lapsed')
        suspended = status == 'suspended'
        denied = status == 'denied'
        inactive = withdrawn or expired or suspended or denied or status in (
            'inactive', 'closed', 'surrendered')
        capacity = next((rec['capacity_mw'] for rec in reversed(records_only)
                         if rec.get('capacity_mw') is not None), None)
        period = next((rec.get('applicable_period') for rec in reversed(records_only)
                       if rec.get('applicable_period')), None)
        withdrawal_period = withdrawal.get('applicable_period') if withdrawal else None
        permits.append({
            'permit_number': number,
            'status': status,
            'active': not inactive,
            'withdrawn': withdrawn,
            'expired': expired,
            'suspended': suspended,
            'denied': denied,
            'phase': latest.get('phase'),
            'capacity_mw': capacity,
            'applicable_period': period,
            'withdrawal_period': withdrawal_period,
            'withdrawal_revision': withdrawal.get('version') if withdrawal else None,
            'revision': latest.get('version'),
            'source_sha256': latest.get('source_sha256'),
            'status_source': ('withdrawal' if withdrawal else
                              'permit' if latest.get('status') else 'phase'),
            'evidence': [_ref(rec) for rec in group],
        })
    permits.sort(key=lambda p: permit_numbers.index(p['permit_number'])
                 if p['permit_number'] in permit_numbers else 10 ** 6)

    active_permits = [p for p in permits if p['active']]
    no_active_permit = bool(permits) and not active_permits
    withdrawal_present = any(p['withdrawn'] for p in permits)
    expiry_only = no_active_permit and not withdrawal_present and any(
        p['expired'] for p in permits)
    suspension_only = no_active_permit and not withdrawal_present and any(
        p['suspended'] for p in permits)
    denial_only = (no_active_permit and not withdrawal_present and
                   not expiry_only and not suspension_only and
                   any(p['denied'] for p in permits))
    permit_capacity = next((p['capacity_mw'] for p in reversed(permits)
                            if p['capacity_mw'] is not None), None)
    active_permit_capacity = next(
        (p['capacity_mw'] for p in reversed(active_permits)
         if p['capacity_mw'] is not None), None)
    equipment = _equipment(recs)
    equipment_capacity = next(
        (item['capacity_mw'] for item in reversed(equipment)
         if item['status'] == 'active' and item['capacity_mw'] is not None),
        next((item['capacity_mw'] for item in reversed(equipment)
              if item['capacity_mw'] is not None), None))
    capacity_note = None
    if (active_permit_capacity is not None and equipment_capacity is not None and
            active_permit_capacity != equipment_capacity):
        capacity_note = ('许可容量与当前设备铭牌容量不同，分别保留；'
                         '设备换型时以铭牌记录为准核对')
    if active_permit_capacity is not None:
        capacity, basis, capacity_status = (active_permit_capacity,
                                            'permit', 'permitted')
    elif equipment_capacity is not None:
        capacity, basis, capacity_status = (equipment_capacity,
                                            'equipment', 'nameplate')
    elif permit_capacity is not None:
        capacity = permit_capacity
        if withdrawal_present:
            basis, capacity_status = 'permit_withdrawn', 'withdrawn'
        elif expiry_only:
            basis, capacity_status = 'permit_expired', 'expired'
        elif denial_only:
            basis, capacity_status = 'permit_denied', 'denied'
        else:
            basis, capacity_status = 'permit_inactive', 'suspended'
    else:
        capacity, basis, capacity_status = None, None, 'unknown'

    phase_evidence = {}
    for rec in recs:
        if rec.get('phase'):
            phase_evidence.setdefault(rec['phase'], []).append(_ref(rec))
        elif rec['kind'] == 'withdrawal':
            phase_evidence.setdefault('withdrawn', []).append(_ref(rec))
    lifecycle = [p for p in phase_evidence if p in PHASE_ORDER]
    phase = max(lifecycle, key=lambda p: PHASE_ORDER[p]) if lifecycle else None
    unknown_phases = []
    for rec in recs:
        raw_phase = rec.get('phase_raw')
        if not raw_phase or rec.get('phase') or raw_phase in unknown_phases:
            continue
        if _withdrawn(raw_phase) or _key(raw_phase) in _SUSPENDED:
            continue
        if rec['kind'] == 'withdrawal':
            continue
        unknown_phases.append(raw_phase)
    if phase is None and not unknown_phases:
        if equipment:
            phase = 'installed'
        elif active_permits:
            phase = 'approved'
    status = ('withdrawn' if no_active_permit and withdrawal_present else
              'expired' if expiry_only else
              'suspended' if suspension_only else
              'denied' if denial_only else phase)

    generation_records, operational, trial = _generation(recs)
    seen_periods = {}
    for item in generation_records:
        if item['phase'] not in ('trial', 'operational'):
            continue
        label = (item.get('period') or {}).get('label')
        if not label:
            continue
        key = (item['phase'], label)
        if key in seen_periods and seen_periods[key] != item['mwh']:
            gaps.append(f'{label} 的运行申报存在不同数值，保留两条并标记')
        seen_periods.setdefault(key, item['mwh'])
    operational_evidence = [rec for rec in recs if rec.get('phase') == 'operational']
    if operational:
        actual_generation = sum(item['mwh'] for item in operational)
        generation_status = 'measured'
    elif operational_evidence:
        actual_generation = None
        generation_status = 'operational_without_measured_generation'
        gaps.append('正式运行缺少实测发电量')
    else:
        actual_generation = None
        generation_status = 'no_operational_filing'
    trial_generation = sum(item['mwh'] for item in trial) if trial else None
    facility = _facility(recs, gaps)
    late = _late(recs)

    if not permits:
        gaps.append('缺少许可来源，容量依据不明')
    if withdrawal_present and no_active_permit:
        gaps.append('许可已撤回，许可容量不能作为实际发电')
    elif withdrawal_present:
        gaps.append('部分许可已撤回，按有效许可容量展示')
    for permit in permits:
        if not permit['withdrawn']:
            continue
        withdrawal_period = permit.get('withdrawal_period') or {}
        withdrawn_to = withdrawal_period.get('end') or withdrawal_period.get('start')
        if not withdrawn_to:
            continue
        for item in generation_records:
            if item['phase'] != 'operational':
                continue
            period = item.get('period') or {}
            if period.get('start') and str(period['start']) > str(withdrawn_to):
                gaps.append('许可撤回后仍有正式运行申报')
                break
    if expiry_only:
        gaps.append('许可已过期，容量仅为历史许可容量')
    if suspension_only:
        gaps.append('许可已暂停，容量不能视为当前有效')
    if denial_only:
        gaps.append('许可申请被拒，容量不能视为有效')
    if active_permits and withdrawal_present:
        gaps.append('部分许可已撤回，按有效许可容量展示')
    if not any(rec.get('applicable_period') for rec in recs):
        gaps.append('缺少适用期间')
    for raw_phase in unknown_phases:
        gaps.append(f'阶段不明：{raw_phase}')
    if facility['status'] != 'resolved':
        gaps.append('设施归属未确认')
    gaps = list(dict.fromkeys(gaps))
    publishable = bool(
        phase == 'operational' and actual_generation is not None and
        facility['status'] == 'resolved' and active_permits)

    return {
        'unit_id': unit_id,
        'unit_ids': unit_ids,
        'equipment_ids': equipment_ids,
        'permit_numbers': permit_numbers,
        'fuel': fuel,
        'fuel_candidates': fuels,
        'phase': phase,
        'phase_raw_candidates': unknown_phases,
        'status': status,
        'phases': {name: refs for name, refs in sorted(
            phase_evidence.items(),
            key=lambda item: PHASE_ORDER.get(item[0], 99))},
        'permit_status': ('withdrawn' if no_active_permit and withdrawal_present else
                          'expired' if expiry_only else
                          'suspended' if suspension_only else
                          'denied' if denial_only else
                          'active' if active_permits else None),
        'permits': permits,
        'permit_capacity_mw': permit_capacity,
        'active_permit_capacity_mw': active_permit_capacity,
        'equipment_capacity_mw': equipment_capacity,
        'capacity_note': capacity_note,
        'capacity_history': sorted([
            {'capacity_mw': p['capacity_mw'], 'basis': 'permit',
             'permit_number': p['permit_number'], 'status': p['status'],
             'applicable_period': p['applicable_period'],
             'source_sha256': p['source_sha256']}
            for p in permits if p['capacity_mw'] is not None] + [
            {'capacity_mw': item['capacity_mw'], 'basis': 'equipment',
             'equipment_id': item['equipment_id'], 'status': item['status'],
             'applicable_period': item['applicable_period'],
             'source_sha256': item['source_sha256']}
            for item in equipment if item['capacity_mw'] is not None],
            key=lambda item: (str((item.get('applicable_period') or {}).get('start') or ''),
                              item['basis'], str(item.get('equipment_id') or
                                                 item.get('permit_number') or ''))),
        'capacity_mw': capacity,
        'capacity_basis': basis,
        'capacity_status': capacity_status,
        'actual_generation_mwh': actual_generation,
        'generation_status': generation_status,
        'trial_generation_mwh': trial_generation,
        'generation_records': generation_records,
        'generation_periods': len(operational),
        'equipment': equipment,
        'facility': facility,
        'gaps': gaps,
        'late_arrival_records': sorted(late),
        'late_arrival_notes': {
            _record_key(rec): rec['late_reason'] for rec in recs
            if _record_key(rec) in late and rec.get('late_reason')} or {
            _record_key(rec): 'arrived_after_later_applicable_period'
            for rec in recs if _record_key(rec) in late},
        'publishable': publishable,
        'evidence': [_ref(rec) for rec in recs],
    }


def assess(rows):
    records = []
    for row in rows:
        rec = normalize(row.get('payload') or row)
        rec.update({'type': row.get('type'), 'id': row.get('id'),
                    'version': row.get('version'),
                    'source_sha256': row.get('source'),
                    'source_url': row.get('source_url'),
                    'received_order': row.get('sequence')})
        if not rec.get('kind'):
            rec['kind'] = ('filing' if rec['type'] == 'support' else
                           'equipment' if rec['type'] == 'equipment' else
                           'withdrawal' if rec['type'] == 'withdrawal' else 'permit')
        if not rec.get('status') and rec['kind'] == 'withdrawal':
            rec['status'] = 'withdrawn'
        records.append(rec)
    units = [_unit(group, []) for group in _group(records)]
    units.sort(key=lambda item: str(item['unit_id']))
    permit_units = {}
    for rec in records:
        if rec.get('permit_number') and rec.get('unit_id'):
            permit_units.setdefault(rec['permit_number'], set()).add(rec['unit_id'])
    ambiguous_permits = sorted(number for number, ids in permit_units.items()
                               if len(ids) > 1)
    primary = [r for r in records if r['kind'] == 'permit']
    support = [r for r in records if r['kind'] in ('filing', 'equipment')]
    reasons = []
    gaps = []
    for unit in units:
        reasons.extend(unit['gaps'])
    if not units:
        reasons.append('缺少任何机组来源')
    if not primary:
        reasons.append('缺少主要来源')
    if not support:
        reasons.append('缺少关联来源')
    if not any(unit['phase'] == 'operational' and unit['actual_generation_mwh'] is not None
               for unit in units):
        reasons.append('正式运行与实际发电量尚无证据')
    if any(unit['permit_status'] == 'withdrawn' for unit in units):
        reasons.append('许可已撤回，许可容量不能作为实际发电')
    for number in ambiguous_permits:
        reasons.append(f'许可 {number} 关联多个机组，未强制归并')
        if f'许可 {number} 关联多个机组，未强制归并' not in gaps:
            gaps.append(f'许可 {number} 关联多个机组，未强制归并')
    if any(unit['permit_status'] == 'expired' for unit in units):
        reasons.append('许可已过期，容量仅为历史许可容量')
    if any(unit['permit_status'] == 'suspended' for unit in units):
        reasons.append('许可已暂停，容量不能视为当前有效')
    if any(unit['permit_status'] == 'denied' for unit in units):
        reasons.append('许可申请被拒，容量不能视为有效')
    for unit in units:
        for gap in unit['gaps']:
            if gap not in gaps:
                gaps.append(gap)
    reasons = list(dict.fromkeys(reasons))
    candidates = [
        {'unit_id': unit['unit_id'], 'status': unit['facility']['status'],
         'facility_candidates': unit['facility']['candidates'],
         'gaps': unit['gaps']}
        for unit in units if unit['facility']['status'] != 'resolved'
    ]
    facilities = {}
    for unit in units:
        if unit['facility']['status'] == 'resolved':
            facilities.setdefault(unit['facility']['facility_id'], []).append(
                unit['unit_id'])
    return {
        'publishable': bool(units) and all(unit['publishable'] for unit in units),
        'publishable_unit_count': sum(unit['publishable'] for unit in units),
        'reasons': reasons,
        'gaps': gaps,
        'primary_count': len(primary),
        'support_count': len(support),
        'record_count': len(records),
        'unit_count': len(units),
        'units': units,
        'facilities': [{'facility_id': key, 'units': value}
                       for key, value in sorted(facilities.items())],
        'candidates': candidates,
    }
