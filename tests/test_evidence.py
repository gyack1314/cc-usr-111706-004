"""核对许可、设备、运行申报的解释边界与保留规则。"""
import base64
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from capture_service.domain import assess, decode, normalize
from capture_service.ledger import attachments, connect, entities, receive
from capture_service.transport import Replay
from capture_service.view import overview

ROOT = Path(__file__).resolve().parents[1]
COLLECTION = 'generators'


def run(manifest, collection=COLLECTION):
    replay = Replay(ROOT / 'data' / manifest)
    db = connect(':memory:')
    for step in replay.steps:
        request = step['request']
        raw, receipt = replay.request(request['method'], request['url'])
        receive(db, collection, raw, receipt)
    return db, overview(db, collection)


def raw(value):
    return json.dumps(value, ensure_ascii=False).encode('utf-8')


def receipt_for(body, url='https://permit.example.test/x'):
    return {'method': 'GET', 'url': url, 'status': 200,
            'body_sha256': hashlib.sha256(body).hexdigest()}


class Interpret(unittest.TestCase):
    def test_phases_are_ordered_and_separate(self):
        permit = normalize({'permit': 'P', 'generator': 'G', 'phase': 'approved',
                            'capacity_mw': 10})
        trial = normalize({'filing': 'R', 'generator': 'G', 'phase': 'trial',
                           'period': '2025-03'})
        self.assertEqual(permit['phase'], 'approved')
        self.assertEqual(permit['capacity_basis'], 'permit')
        self.assertIsNone(trial['capacity_mw'])
        self.assertEqual(trial['applicable_period']['label'], '2025-03')

    def test_capacity_units_and_generation_are_not_interchangeable(self):
        row = normalize({'permit': 'P', 'generator': 'G', 'capacity': '1,200 kW'})
        self.assertEqual(row['capacity_mw'], 1.2)
        filed = normalize({'filing': 'R', 'generator': 'G', 'mwh': 15})
        self.assertEqual(filed['generation_mwh'], 15)
        self.assertIsNone(filed['capacity_mw'])

    def test_unknown_phase_is_not_guessed(self):
        row = normalize({'permit': 'P', 'generator': 'G', 'phase': 'pending review'})
        self.assertIsNone(row['phase'])
        view = assess([{'type': 'generator', 'id': 'P', 'version': 'r1',
                        'source': 's',
                        'payload': {'permit': 'P', 'generator': 'G',
                                    'revision': 'r1', 'phase': 'pending review'}}])
        unit = view['units'][0]
        self.assertIsNone(unit['phase'])
        self.assertIn('阶段不明：pending review', unit['gaps'])

    def test_withdrawal_is_a_status_not_a_phase(self):
        row = normalize({'permit': 'P', 'generator': 'G', 'status': 'revoked',
                         'capacity_mw': 8})
        self.assertEqual(row['kind'], 'permit')
        self.assertEqual(row['status'], 'withdrawn')
        self.assertTrue(row['withdrawn'])
        self.assertIsNone(row['phase'])
        notice = normalize({'permit': 'P', 'generator': 'G',
                            'phase': 'withdrawn', 'revision': 'r1'})
        self.assertEqual(notice['kind'], 'withdrawal')
        self.assertIsNone(notice['phase'])

    def test_denied_permit_is_not_withdrawn(self):
        rows = [{'type': 'generator', 'id': 'P', 'version': 'r1', 'source': 's',
                 'payload': {'permit': 'P', 'generator': 'G', 'revision': 'r1',
                             'status': 'denied', 'capacity_mw': 5,
                             'site': 'F', 'fuel': 'gas', 'period': '2025'}}]
        unit = assess(rows)['units'][0]
        self.assertEqual(unit['permit_status'], 'denied')
        self.assertEqual(unit['capacity_basis'], 'permit_denied')
        self.assertNotIn('许可已撤回，许可容量不能作为实际发电', unit['gaps'])

    def test_unknown_unit_is_not_read_as_megawatts(self):
        row = normalize({'permit': 'P', 'generator': 'G', 'capacity': '5 tons'})
        self.assertIsNone(row['capacity_mw'])

    def test_duplicate_conflicting_rows_in_one_response_are_rejected(self):
        body = raw({'revision': 'r1', 'records': [
            {'record_type': 'permit', 'permit_number': 'P', 'unit_id': 'G',
             'capacity_mw': 5},
            {'record_type': 'permit', 'permit_number': 'P', 'unit_id': 'G',
             'capacity_mw': 6}]})
        db = connect(':memory:')
        with self.assertRaises(ValueError):
            receive(db, COLLECTION, body, receipt_for(body))
        self.assertEqual(db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 0)
        db.close()

    def test_batch_catalog_keeps_identity_and_revision(self):
        rows = decode(raw({'revision': 'r9', 'records': [
            {'record_type': 'permit', 'permit_number': 'P-1', 'unit_id': 'G-1'},
            {'record_type': 'permit', 'permit_number': 'P-2', 'unit_id': 'G-2'}]}),
            receipt_for(b''))
        self.assertEqual([row['id'] for row in rows], ['P-1', 'P-2'])
        self.assertEqual({row['version'] for row in rows}, {'r9'})
        self.assertEqual([row['type'] for row in rows], ['generator', 'generator'])


class Complete(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db, cls.view = run('complete.json')
        cls.unit = cls.view['units'][0]

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_stages_are_retained_separately(self):
        self.assertEqual(self.unit['phase'], 'operational')
        self.assertEqual(set(self.unit['phases']),
                         {'approved', 'installed', 'trial', 'operational'})
        self.assertEqual(self.unit['permit_status'], 'active')
        self.assertEqual(self.unit['permit_capacity_mw'], 25)
        self.assertEqual(self.unit['equipment_capacity_mw'], 24)

    def test_capacity_is_not_generation(self):
        self.assertEqual(self.unit['actual_generation_mwh'], 290)
        self.assertEqual(self.unit['trial_generation_mwh'], 12)
        self.assertEqual(self.unit['capacity_mw'], 25)
        self.assertEqual(self.unit['generation_status'], 'measured')

    def test_complete_chain_is_publishable_and_facility_resolved(self):
        self.assertTrue(self.view['publishable'])
        self.assertEqual(self.unit['facility']['status'], 'resolved')
        self.assertEqual(self.unit['facility']['facility_id'], 'FAC-3')
        self.assertEqual(self.view['reasons'], [])

    def test_original_attachment_is_stored_with_digest(self):
        stored = attachments(self.db, COLLECTION, 'generator', 'PER-3', 'r1')
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]['name'], 'permit-PER-3.pdf')
        blob = attachments(self.db, COLLECTION, 'generator', 'PER-3', 'r1',
                           include_content=True)[0]['raw']
        self.assertTrue(blob.startswith(b'%PDF'))
        self.assertEqual(stored[0]['sha256'], hashlib.sha256(blob).hexdigest())

    def test_unstored_attachment_stays_declared(self):
        record = next(r for r in self.view['records'] if r['id'] == 'PER-3')
        self.assertEqual([a['name'] for a in record['declared_attachments']],
                         ['site-plan.pdf'])


class Lifecycle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db, cls.view = run('lifecycle.json')
        cls.unit = cls.view['units'][0]

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_withdrawal_blocks_permit_capacity_but_keeps_generation(self):
        self.assertEqual(self.unit['permit_status'], 'withdrawn')
        self.assertEqual(self.unit['permit_capacity_mw'], 20)
        self.assertEqual(self.unit['capacity_basis'], 'equipment')
        self.assertEqual(self.unit['capacity_status'], 'nameplate')
        self.assertEqual(self.unit['actual_generation_mwh'], 120)
        self.assertFalse(self.unit['publishable'])
        self.assertIn('许可已撤回，许可容量不能作为实际发电', self.unit['gaps'])

    def test_capacity_history_keeps_withdrawn_and_replaced_values(self):
        values = {(item['capacity_mw'], item['basis'], item['status'])
                  for item in self.unit['capacity_history']}
        self.assertIn((20, 'permit', 'withdrawn'), values)
        self.assertIn((18, 'equipment', 'superseded'), values)
        self.assertIn((20, 'equipment', 'active'), values)

    def test_equipment_change_keeps_both_units(self):
        history = {item['equipment_id']: item for item in self.unit['equipment']}
        self.assertEqual(set(history), {'EQ-40', 'EQ-41'})
        self.assertEqual(history['EQ-40']['status'], 'superseded')
        self.assertEqual(history['EQ-40']['change'], 'replaced')
        self.assertEqual(history['EQ-40']['superseded_by']['id'], 'EQ-41')
        self.assertEqual(history['EQ-40']['capacity_change_mw'], 2)
        self.assertEqual(history['EQ-41']['status'], 'active')

    def test_old_permit_is_not_flagged_late(self):
        self.assertNotIn('generator:PER-4:r1', self.unit['late_arrival_records'])

    def test_late_filing_is_flagged_not_dropped(self):
        self.assertEqual(self.unit['late_arrival_records'], ['support:RUN-41:r1'])
        ids = {item['id'] for item in self.unit['generation_records']}
        self.assertIn('RUN-41', ids)
        self.assertEqual(len(entities(self.db, COLLECTION)), 6)


class Ambiguity(unittest.TestCase):
    def test_multiple_facility_candidates_are_kept(self):
        db, view = run('ambiguous.json')
        unit = next(u for u in view['units'] if u['unit_id'] == 'GEN-5')
        self.assertEqual(unit['facility']['status'], 'ambiguous')
        self.assertEqual(unit['facility']['candidate_ids'], ['FAC-A', 'FAC-B'])
        self.assertIsNone(unit['facility']['facility_id'])
        self.assertIn('设施归属存在多个候选，未强制归并', view['gaps'])
        db.close()

    def test_missing_facility_stays_a_gap(self):
        db, view = run('ambiguous.json')
        unit = next(u for u in view['units'] if u['unit_id'] == 'GEN-6')
        self.assertEqual(unit['facility']['status'], 'unresolved')
        self.assertIn('设施归属不明：缺少设施标识', unit['gaps'])
        self.assertFalse(view['publishable'])
        db.close()


class Catalog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db, cls.view = run('catalog.json')
        cls.units = {unit['unit_id']: unit for unit in cls.view['units']}

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_attachment_url_must_be_unique(self):
        db = connect(':memory:')
        for name, unit in (('one', 'G-1'), ('two', 'G-2')):
            body = raw({'permit': 'P-' + name, 'generator': unit,
                        'revision': 'r1',
                        'attachments': [{'name': 'x.pdf',
                                         'url': 'https://permit.example.test/x.pdf'}]})
            receive(db, COLLECTION, body, receipt_for(body))
        blob = b'%PDF shared'
        receipt = receipt_for(blob, 'https://permit.example.test/x.pdf')
        receive(db, COLLECTION, blob, receipt)
        self.assertEqual(attachments(db, COLLECTION), [])
        self.assertEqual(db.execute(
            'SELECT COUNT(*) FROM receipts').fetchone()[0], 3)
        view = overview(db, COLLECTION)
        self.assertEqual(len(view['unlinked_receipts']), 1)
        self.assertIn('有来源原文未关联到记录', view['gaps'])
        self.assertFalse(view['publishable'])
        db.close()

    def test_catalog_records_split_into_units(self):
        self.assertEqual(set(self.units), {'GEN-7', 'GEN-8', 'GEN-9'})
        self.assertEqual(self.units['GEN-7']['capacity_mw'], 9.5)
        self.assertEqual(self.units['GEN-9']['capacity_mw'], 1.2)

    def test_expired_permit_is_not_treated_as_withdrawal(self):
        unit = self.units['GEN-9']
        self.assertEqual(unit['permit_status'], 'expired')
        self.assertEqual(unit['capacity_basis'], 'permit_expired')
        self.assertNotIn('许可已撤回，许可容量不能作为实际发电', unit['gaps'])

    def test_suspended_permit_is_not_withdrawn_or_usable(self):
        rows = [{'type': 'generator', 'id': 'P', 'version': 'r1', 'source': 's',
                 'payload': {'permit': 'P', 'generator': 'G', 'revision': 'r1',
                             'status': 'suspended', 'phase': 'approved',
                             'capacity_mw': 5, 'site': 'F', 'fuel': 'gas',
                             'period': '2025'}}]
        unit = assess(rows)['units'][0]
        self.assertEqual(unit['permit_status'], 'suspended')
        self.assertEqual(unit['capacity_basis'], 'permit_inactive')
        self.assertNotIn('许可已撤回，许可容量不能作为实际发电', unit['gaps'])

    def test_generation_from_catalog_filing(self):
        unit = self.units['GEN-8']
        self.assertEqual(unit['actual_generation_mwh'], 210)
        self.assertEqual(unit['generation_status'], 'measured')


class Attachments(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db, cls.view = run('attachment.json')

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_attachment_step_is_linked_to_entity(self):
        stored = attachments(self.db, COLLECTION, 'generator', 'PER-6', 'r1')
        self.assertEqual([item['name'] for item in stored],
                         ['permit-PER-6.pdf'])
        record = next(r for r in self.view['records'] if r['id'] == 'PER-6')
        self.assertEqual(record['attachments'][0]['sha256'], stored[0]['sha256'])

    def test_inline_attachment_digest_must_match(self):
        body = raw({'permit': 'P', 'generator': 'G', 'revision': 'r1',
                    'attachments': [{'name': 'a.pdf', 'sha256': '0' * 64,
                                     'content_base64': base64.b64encode(b'x').decode()}]})
        db = connect(':memory:')
        with self.assertRaises(ValueError):
            receive(db, COLLECTION, body, receipt_for(body))
        self.assertEqual(db.execute('SELECT COUNT(*) FROM entities').fetchone()[0], 0)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 0)
        db.close()


class AttachmentByUrl(unittest.TestCase):
    def test_null_content_is_still_declared(self):
        db, view = run('attachment.json')
        record = next(r for r in view['records'] if r['id'] == 'WDR-6')
        self.assertEqual([item['name'] for item in record['declared_attachments']],
                         ['revocation-letter.pdf'])
        db.close()

    def test_orphan_attachment_is_stored_and_flagged(self):
        db = connect(':memory:')
        blob = b'%PDF orphan'
        receipt = receipt_for(blob, 'https://permit.example.test/orphan.pdf')
        receipt.update({'content_type': 'application/pdf',
                        'attachment_of': {'type': 'generator', 'id': 'G-9',
                                          'version': 'r1', 'name': 'orphan.pdf'}})
        receive(db, COLLECTION, blob, receipt)
        view = overview(db, COLLECTION)
        self.assertEqual(len(view['unlinked_receipts']), 1)
        self.assertFalse(view['publishable'])
        db.close()

    def test_attachment_step_matches_declared_url(self):
        db, view = run('attachment-url.json')
        record = next(r for r in view['records'] if r['id'] == 'PER-12')
        self.assertEqual([item['name'] for item in record['attachments']],
                         ['permit-PER-12.pdf'])
        self.assertEqual(record['declared_attachments'][0]['name'],
                         'permit-PER-12.pdf')
        stored = attachments(db, COLLECTION, 'generator', 'PER-12', 'r1',
                             include_content=True)
        self.assertTrue(stored[0]['raw'].startswith(b'%PDF'))
        db.close()


class Persistence(unittest.TestCase):
    def test_withdrawal_in_same_revision_is_kept_alongside_permit(self):
        db = connect(':memory:')
        status_body = raw({'permit': 'P-1', 'generator': 'G-1', 'revision': 'r1',
                           'status': 'active', 'phase': 'approved',
                           'capacity_mw': 10, 'site': 'F', 'fuel': 'gas'})
        receive(db, COLLECTION, status_body, receipt_for(status_body))
        revoke_body = raw({'kind': 'withdrawal', 'withdrawal': 'W-1',
                           'permit': 'P-1', 'generator': 'G-1',
                           'revision': 'r1', 'status': 'revoked'})
        receive(db, COLLECTION, revoke_body, receipt_for(revoke_body))
        view = overview(db, COLLECTION)
        self.assertEqual(view['record_count'], 2)
        self.assertEqual(view['units'][0]['permit_status'], 'withdrawn')
        self.assertEqual(view['units'][0]['permit_capacity_mw'], 10)
        self.assertEqual(view['units'][0]['capacity_basis'], 'permit_withdrawn')
        db.close()

    def test_records_survive_reopen_and_new_revision_appends(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'ledger.sqlite')
            db = connect(path)
            first = raw({'permit': 'P-1', 'generator': 'G-1', 'revision': 'r1',
                         'capacity_mw': 10, 'phase': 'approved'})
            receive(db, COLLECTION, first, receipt_for(first))
            second = raw({'permit': 'P-1', 'generator': 'G-1', 'revision': 'r2',
                          'capacity_mw': 10, 'phase': 'installed'})
            receive(db, COLLECTION, second, receipt_for(second))
            db.close()
            db = connect(path)
            view = overview(db, COLLECTION)
            self.assertEqual(view['record_count'], 2)
            self.assertEqual(view['units'][0]['phase'], 'installed')
            self.assertEqual(len(view['units'][0]['phases']), 2)
            db.close()

    def test_withdrawal_revision_does_not_delete_permit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'ledger.sqlite')
            db = connect(path)
            permit = raw({'permit': 'P-2', 'generator': 'G-2', 'revision': 'r1',
                          'capacity_mw': 7, 'phase': 'approved', 'site': 'F',
                          'fuel': 'gas'})
            receive(db, COLLECTION, permit, receipt_for(permit))
            before = overview(db, COLLECTION)
            withdrawal = raw({'withdrawal': 'W-2', 'permit': 'P-2',
                              'generator': 'G-2', 'revision': 'r1',
                              'status': 'revoked'})
            receive(db, COLLECTION, withdrawal, receipt_for(withdrawal))
            after = overview(db, COLLECTION)
            self.assertEqual(after['record_count'], 2)
            self.assertEqual(len(after['units'][0]['permits']), 1)
            self.assertEqual(after['units'][0]['permits'][0]['capacity_mw'], 7)
            self.assertEqual(after['units'][0]['permit_status'], 'withdrawn')
            self.assertIn('许可已撤回，许可容量不能作为实际发电',
                          after['units'][0]['gaps'])
            self.assertEqual(before['units'][0]['permit_status'], 'active')
            db.close()

    def test_partial_withdrawal_keeps_active_permit_capacity(self):
        bodies = [
            {'permit': 'P-10', 'generator': 'G-10', 'revision': 'r1',
             'phase': 'approved', 'capacity_mw': 10, 'site': 'F',
             'fuel': 'gas', 'period': '2025'},
            {'permit': 'P-11', 'generator': 'G-10', 'revision': 'r1',
             'phase': 'approved', 'capacity_mw': 12, 'site': 'F',
             'fuel': 'gas', 'period': '2026'},
            {'withdrawal': 'W-10', 'permit': 'P-10', 'generator': 'G-10',
             'revision': 'r1', 'status': 'revoked', 'period': '2025-05'},
        ]
        db = connect(':memory:')
        for body in bodies:
            data = raw(body)
            receive(db, COLLECTION, data, receipt_for(data))
        view = overview(db, COLLECTION)
        unit = view['units'][0]
        self.assertEqual(unit['permit_status'], 'active')
        self.assertEqual(unit['active_permit_capacity_mw'], 12)
        self.assertEqual(unit['permit_capacity_mw'], 12)
        self.assertEqual(len(unit['permits']), 2)
        self.assertIn('部分许可已撤回，按有效许可容量展示', unit['gaps'])
        db.close()

    def test_late_filing_after_reopen_is_not_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'ledger.sqlite')
            db = connect(path)
            for body in (
                {'permit': 'P-3', 'generator': 'G-3', 'revision': 'r1',
                 'capacity_mw': 5, 'phase': 'approved'},
                {'filing': 'R-3', 'generator': 'G-3', 'revision': 'r1',
                 'phase': 'operational', 'period': '2025-06', 'mwh': 40},
                {'filing': 'R-2', 'generator': 'G-3', 'revision': 'r1',
                 'phase': 'trial', 'period': '2025-02', 'mwh': 3},
            ):
                data = raw(body)
                receive(db, COLLECTION, data, receipt_for(data))
            view = overview(db, COLLECTION)
            self.assertEqual(view['record_count'], 3)
            self.assertEqual(view['units'][0]['late_arrival_records'],
                             ['support:R-2:r1'])
            self.assertEqual(view['units'][0]['actual_generation_mwh'], 40)
            db.close()

    def test_old_schema_database_is_migrated_without_data_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'legacy.sqlite')
            legacy = sqlite3.connect(path)
            legacy.executescript("""
                CREATE TABLE receipts(collection TEXT NOT NULL,body_sha256 TEXT NOT NULL,
                    source_url TEXT NOT NULL,status INTEGER NOT NULL,raw BLOB NOT NULL,
                    PRIMARY KEY(collection,body_sha256));
                CREATE TABLE entities(collection TEXT NOT NULL,entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,version TEXT NOT NULL,
                    receipt_sha256 TEXT NOT NULL,payload_json TEXT NOT NULL,
                    PRIMARY KEY(collection,entity_type,entity_id,version));
            """)
            body = raw({'generator': 'GEN-1', 'revision': 'r1', 'permit': 'PER-1',
                        'site': 'SITE-1', 'fuel': 'gas', 'mw': 15,
                        'phase': 'approved'})
            sha = hashlib.sha256(body).hexdigest()
            legacy.execute('INSERT INTO receipts VALUES(?,?,?,?,?)',
                           (COLLECTION, sha, 'https://permit.example.test/x', 200, body))
            legacy.execute('INSERT INTO entities VALUES(?,?,?,?,?,?)',
                           (COLLECTION, 'generator', 'GEN-1', 'r1', sha,
                            json.dumps({'generator': 'GEN-1', 'revision': 'r1',
                                        'permit': 'PER-1', 'site': 'SITE-1',
                                        'fuel': 'gas', 'mw': 15,
                                        'phase': 'approved'})))
            legacy.commit()
            legacy.close()
            db = connect(path)
            view = overview(db, COLLECTION)
            self.assertEqual(view['record_count'], 1)
            self.assertEqual(view['units'][0]['capacity_mw'], 15)
            db.close()


class Rollback(unittest.TestCase):
    def test_conflicting_revision_does_not_partially_write(self):
        replay = Replay(ROOT / 'data/conflict.json')
        db = connect(':memory:')
        req = replay.steps[0]['request']
        body, receipt = replay.request(req['method'], req['url'])
        receive(db, COLLECTION, body, receipt)
        before = overview(db, COLLECTION)
        req = replay.steps[1]['request']
        body, receipt = replay.request(req['method'], req['url'])
        with self.assertRaises(ValueError):
            receive(db, COLLECTION, body, receipt)
        self.assertEqual(overview(db, COLLECTION), before)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 1)
        db.close()

    def test_bad_attachment_digest_rolls_back_whole_response(self):
        body = raw({'permit': 'P-9', 'generator': 'G-9', 'revision': 'r1',
                    'attachments': [{'name': 'a.pdf', 'sha256': 'f' * 64,
                                     'content_base64': base64.b64encode(b'x').decode()}]})
        db = connect(':memory:')
        with self.assertRaises(ValueError):
            receive(db, COLLECTION, body, receipt_for(body))
        self.assertEqual(db.execute('SELECT COUNT(*) FROM entities').fetchone()[0], 0)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 0)
        db.close()


if __name__ == '__main__':
    unittest.main()
