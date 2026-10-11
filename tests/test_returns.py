import asyncio
import base64
import copy
import threading
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from ordinet import NS, LegacyError, OrdiNet
from returns import ReturnRequest, ReturnService
from tools import ToolError

NUMBER = 'GS-2026-000117'
EMAIL = 'customer@example.com'
KEY = 'unit-test-hash-key-' * 3
NOW = datetime(2026, 10, 7, 10, tzinfo=timezone.utc)


def b64(value):
    return base64.b64encode(value.encode('utf-8')).decode('ascii')


class MemoryCAS:
    def __init__(self):
        self.rows = {}
        self.fail_write = False
        self.fail_final = False
        self.lock = threading.Lock()

    def get(self, partition):
        with self.lock:
            return copy.deepcopy(self.rows.get(partition))

    def insert(self, partition, data):
        with self.lock:
            if partition in self.rows:
                return False
            self.rows[partition] = (copy.deepcopy(data), 1)
            return True

    def replace(self, partition, data, etag):
        with self.lock:
            if self.fail_write or (self.fail_final and data['state'] in {'submitted', 'submission_unknown'}):
                raise RuntimeError('storage unavailable')
            if self.rows.get(partition, (None, None))[1] != etag:
                return False
            self.rows[partition] = (copy.deepcopy(data), etag + 1)
            return True


class FakeOrder:
    def __init__(self):
        self.calls = 0
        self.xml = ET.fromstring('<Ordine><NumeroOrdine>000117</NumeroOrdine><Email>' + EMAIL + '</Email><StatoCod>ST05</StatoCod><DataConsegna>06/10/2026</DataConsegna><Righe><Riga><CodArt>OT-001</CodArt><Qta>2</Qta></Riga></Righe></Ordine>')
        self.create_return = AsyncMock(return_value='RMA-test-001')

    async def order(self, number):
        self.calls += 1
        return copy.deepcopy(self.xml)


class ReturnTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = MemoryCAS()
        self.now = NOW
        self.service = ReturnService(self.store, KEY, NUMBER, EMAIL, clock=lambda: self.now, submission_enabled=True)
        self.order = FakeOrder()
        self.request = SimpleNamespace(headers={'X-Chat-User-Ref': 'unit-visitor', 'X-Chat-Message-Id': 'prepare', 'X-Chat-User-Message-B64': b64('Vorrei restituire un articolo')})
        self.scope = {'order_number': NUMBER, 'email': EMAIL}
        self.body = ReturnRequest(**self.scope, operation='prepare', lines=[{'product_id': 'OT-001', 'quantity': 1}], reason='damaged')

    async def prepare(self):
        return await self.service.prepare(self.request, self.body, self.order)

    def decision(self, draft, operation='approve'):
        self.request.headers.update({'X-Chat-Message-Id': 'decision', 'X-Chat-User-Message-B64': b64('CONFERMO' if operation == 'approve' else 'ANNULLA')})
        return ReturnRequest(**self.scope, operation=operation, draft_id=draft.draft_id)

    async def test_submit_and_replay_once(self):
        draft = await self.prepare()
        body = self.decision(draft)
        result = await self.service.decide(self.request, body, self.order)
        self.assertEqual(result.state, 'submitted')
        self.assertEqual(result.return_id, 'RMA-test-001')
        self.assertTrue(result.decision_recorded)
        self.assertFalse((await self.service.decide(self.request, body, self.order)).decision_recorded)
        self.order.create_return.assert_awaited_once_with(NUMBER, [{'product_id': 'OT-001', 'quantity': 1}], 'damaged')

    async def test_concurrent_decisions_one_sender(self):
        draft = await self.prepare()
        body = self.decision(draft)
        results = await asyncio.gather(*(self.service.decide(self.request, body, self.order) for _ in range(8)))
        self.order.create_return.assert_awaited_once()
        self.assertEqual(sum(r.decision_recorded for r in results), 1)

    async def test_timeout_is_unknown_and_never_retried_after_restart(self):
        draft = await self.prepare()
        body = self.decision(draft)
        self.order.create_return.side_effect = httpx.ReadTimeout('unknown response')
        result = await self.service.decide(self.request, body, self.order)
        self.assertEqual(result.state, 'submission_unknown')
        restarted = ReturnService(self.store, KEY, NUMBER, EMAIL, clock=lambda: NOW, submission_enabled=True)
        self.assertEqual((await restarted.decide(self.request, body, self.order)).state, 'submission_unknown')
        self.order.create_return.assert_awaited_once()

    async def test_final_storage_failure_never_resends(self):
        draft = await self.prepare()
        body = self.decision(draft)
        self.store.fail_final = True
        result = await self.service.decide(self.request, body, self.order)
        self.assertEqual(result.state, 'submission_unknown')
        self.assertEqual(result.return_id, 'RMA-test-001')
        self.assertEqual(self.store.get('order-000117')[0]['state'], 'submitting')
        await self.service.decide(self.request, body, self.order)
        self.order.create_return.assert_awaited_once()

    async def test_write_claim_failure_no_sender(self):
        draft = await self.prepare()
        body = self.decision(draft)
        self.store.fail_write = True
        with self.assertRaises(ToolError):
            await self.service.decide(self.request, body, self.order)
        self.order.create_return.assert_not_called()

    async def test_crash_after_claim_does_not_resend(self):
        draft = await self.prepare()
        body = self.decision(draft)
        self.order.create_return.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.service.decide(self.request, body, self.order)
        result = await self.service.decide(self.request, body, self.order)
        self.assertEqual(result.state, 'submitting')
        self.order.create_return.assert_awaited_once()

    async def test_submission_disabled_stops_without_consuming_proposal(self):
        self.service.submission_enabled = False
        draft = await self.prepare()
        body = self.decision(draft)
        result = await self.service.decide(self.request, body, self.order)
        self.assertTrue(result.submission_stopped)
        self.assertFalse(result.submission_enabled)
        self.assertFalse(result.decision_recorded)
        stored, etag = self.store.get('order-000117')
        self.assertEqual(stored['state'], 'prepared')
        self.assertNotIn('decision_at', stored)
        self.assertNotIn('decision_message_id', stored)
        await self.service.decide(self.request, body, self.order)
        self.assertEqual(self.store.get('order-000117')[1], etag)
        self.service.submission_enabled = True
        self.request.headers['X-Chat-Message-Id'] = 'another-confirmation'
        with self.assertRaises(ToolError):
            await self.service.decide(self.request, body, self.order)
        self.order.create_return.assert_not_called()

    async def test_real_mode_requires_new_draft_and_new_user_confirmation(self):
        self.service.submission_enabled = False
        preview = await self.prepare()
        old_decision = self.decision(preview)
        stopped = await self.service.decide(self.request, old_decision, self.order)
        self.assertTrue(stopped.submission_stopped)
        self.service.submission_enabled = True
        self.request.headers.update({
            'X-Chat-Message-Id': 'request-real-return',
            'X-Chat-User-Message-B64': b64('Voglio la proposta per un reso reale'),
        })
        real = await self.prepare()
        self.assertNotEqual(real.draft_id, preview.draft_id)
        self.assertTrue(real.submission_enabled)
        self.assertTrue(real.summary.startswith('RESO REALE'))
        self.order.create_return.assert_not_called()
        self.request.headers.update({
            'X-Chat-Message-Id': 'fresh-real-confirmation',
            'X-Chat-User-Message-B64': b64('CONFERMO'),
        })
        with self.assertRaises(ToolError):
            await self.service.decide(self.request, old_decision, self.order)
        decision = old_decision.model_copy(update={'draft_id': real.draft_id})
        created = await self.service.decide(self.request, decision, self.order)
        self.assertEqual(created.state, 'submitted')
        self.assertEqual(created.return_id, 'RMA-test-001')
        replay = await self.service.decide(self.request, decision, self.order)
        self.assertFalse(replay.decision_recorded)
        self.order.create_return.assert_awaited_once()

    async def test_unrestricted_preview_requires_purchase_email(self):
        self.service = ReturnService(self.store, KEY, clock=lambda: NOW)
        options = await self.service.options(self.request, self.body, self.order)
        self.assertTrue(options.eligible)
        self.assertFalse(options.submission_enabled)
        with self.assertRaises(ToolError):
            await self.service.options(self.request, self.body.model_copy(update={'email': 'wrong@example.com'}), self.order)
        real = ReturnService(self.store, KEY, submission_enabled=True)
        self.assertTrue(real.submission_enabled)
        with self.assertRaises(ToolError):
            await real.options(self.request, self.body.model_copy(update={'email': 'wrong@example.com'}), self.order)
        with self.assertRaises(ValueError):
            ReturnService(self.store, KEY, allowed_order=NUMBER, submission_enabled=True)

    async def test_unrestricted_real_mode_accepts_two_verified_orders_with_fake_sender(self):
        self.service = ReturnService(self.store, KEY, clock=lambda: self.now, submission_enabled=True)
        for number in (NUMBER, 'GS-2026-000118'):
            self.body = self.body.model_copy(update={'order_number': number})
            self.order.xml.find('NumeroOrdine').text = number.rsplit('-', 1)[-1]
            self.request.headers.update({
                'X-Chat-Message-Id': 'prepare-' + number,
                'X-Chat-User-Message-B64': b64('Vorrei restituire un articolo'),
            })
            draft = await self.prepare()
            self.request.headers.update({
                'X-Chat-Message-Id': 'decision-' + number,
                'X-Chat-User-Message-B64': b64('CONFERMO'),
            })
            decision = ReturnRequest(order_number=number, email=EMAIL, operation='approve', draft_id=draft.draft_id)
            result = await self.service.decide(self.request, decision, self.order)
            self.assertEqual(result.state, 'submitted')
            replay = await self.service.decide(self.request, decision, self.order)
            self.assertFalse(replay.decision_recorded)
        self.assertEqual(self.order.create_return.await_count, 2)
        self.assertEqual(len(self.store.rows), 2)

    async def test_transport_write_guard_before_login_or_http(self):
        async with httpx.AsyncClient() as client:
            client.post = AsyncMock()
            soap = OrdiNet(client, 'unit', 'unit')
            with self.assertRaises(LegacyError):
                await soap.create_return(NUMBER, [{'product_id': 'OT-001', 'quantity': 1}], 'other')
            with self.assertRaises(LegacyError):
                await soap._exchange('CreaRichiestaReso', [])
            client.post.assert_not_called()

    async def test_cancel_only_proposal_no_soap_write(self):
        draft = await self.prepare()
        result = await self.service.decide(self.request, self.decision(draft, 'cancel'), self.order)
        self.assertEqual(result.state, 'cancelled')
        self.assertTrue(result.decision_recorded)
        self.order.create_return.assert_not_called()

    async def test_shipped_order_rejected_without_simulation(self):
        self.order.xml.find('StatoCod').text = 'ST03'
        with self.assertRaises(ToolError):
            await self.prepare()
        self.assertEqual(self.store.rows, {})

    async def test_eligibility_rechecked_before_claim(self):
        draft = await self.prepare()
        self.order.xml.find('StatoCod').text = 'ST07'
        with self.assertRaises(ToolError):
            await self.service.decide(self.request, self.decision(draft), self.order)
        self.assertEqual(self.store.get('order-000117')[0]['state'], 'prepared')
        self.order.create_return.assert_not_called()

    async def test_expiry_and_exact_consent(self):
        draft = await self.prepare()
        body = self.decision(draft)
        for value in ('NON CONFERMO', 'confermo', ' CONFERMO', 'CONFERMO ', 'CONFERMO\n', '"CONFERMO"'):
            self.request.headers['X-Chat-User-Message-B64'] = b64(value)
            with self.assertRaises(ToolError):
                await self.service.decide(self.request, body, self.order)
        self.request.headers['X-Chat-User-Message-B64'] = b64('CONFERMO')
        self.now += timedelta(minutes=16)
        with self.assertRaises(ToolError):
            await self.service.decide(self.request, body, self.order)
        self.order.create_return.assert_not_called()

    async def test_stale_message_wrong_email_and_wrong_draft(self):
        draft = await self.prepare()
        body = self.decision(draft)
        self.request.headers['X-Chat-Message-Id'] = 'prepare'
        with self.assertRaises(ToolError):
            await self.service.decide(self.request, body, self.order)
        self.request.headers.update({'X-Chat-Message-Id': 'decision', 'X-Chat-User-Ref': 'another-visitor'})
        with self.assertRaises(ToolError):
            await self.service.decide(self.request, body.model_copy(update={'email': 'wrong@example.com'}), self.order)
        self.request.headers['X-Chat-User-Ref'] = 'unit-visitor'
        with self.assertRaises(ToolError):
            await self.service.decide(self.request, body.model_copy(update={'draft_id': '0' * 32}), self.order)
        self.order.create_return.assert_not_called()

    async def test_closed_prepare_and_read_status_no_sender(self):
        draft = await self.prepare()
        await self.service.decide(self.request, self.decision(draft), self.order)
        self.request.headers['X-Chat-User-Message-B64'] = b64('Un altro reso')
        with self.assertRaises(ToolError):
            await self.prepare()
        status = await self.service.check_status(self.request, self.body, self.order)
        self.assertFalse(status.decision_recorded)
        self.assertEqual(status.return_id, 'RMA-test-001')
        self.assertEqual((await self.service.options(self.request, self.body, self.order)).existing_state, 'submitted')
        self.order.create_return.assert_awaited_once()

    async def test_wrong_email_and_unknown_order_no_reads(self):
        for changes in ({'email': 'wrong@example.com'}, {'order_number': 'GS-2026-000118'}):
            with self.assertRaises(ToolError):
                await self.service.prepare(self.request, self.body.model_copy(update=changes), self.order)
        self.assertEqual(self.order.calls, 0)

    async def test_line_validation_and_changed_proposal(self):
        for quantity in (0, -1, True, '1', 1.5, 1001):
            with self.assertRaises(ValueError):
                ReturnRequest(**self.scope, operation='prepare', lines=[{'product_id': 'OT-001', 'quantity': quantity}], reason='damaged')
        with self.assertRaises(ValueError):
            ReturnRequest(**self.scope, operation='prepare', lines=[{'product_id': 'OT-001', 'quantity': 1}] * 2, reason='damaged')
        old = await self.prepare()
        self.body = self.body.model_copy(update={'reason': 'other'})
        changed = await self.prepare()
        self.assertNotEqual(old.draft_id, changed.draft_id)
        with self.assertRaises(ToolError):
            await self.service.decide(self.request, self.decision(old), self.order)
        self.order.create_return.assert_not_called()

    async def test_unicode_context_and_malformed_encoding(self):
        for message in ('è 🎁', ' CONFERMO ', 'CONFERMO\n'):
            self.request.headers['X-Chat-User-Message-B64'] = b64(message)
            self.assertEqual(self.service.event(self.request)[0], message)
        for value in ('{{unresolved}}', '/w==', '!bad!'):
            self.request.headers['X-Chat-User-Message-B64'] = value
            with self.assertRaises(ToolError):
                self.service.event(self.request)

    async def test_soap_nested_payload_and_reason_mapping(self):
        seen = []
        def respond(request):
            root = ET.fromstring(request.content)
            seen.append(root)
            return httpx.Response(200, text='<Envelope><Esito><Codice>OK</Codice></Esito><IdReso>RMA-123</IdReso></Envelope>')
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            soap = OrdiNet(client, 'unit', 'unit', writes_enabled=True)
            soap._login = AsyncMock()
            soap.session = 'unit-session'
            identifier = await soap.create_return(NUMBER, [{'product_id': 'OT-001', 'quantity': 1}], 'damaged')
        self.assertEqual(identifier, 'RMA-123')
        self.assertEqual(seen[0].findtext(f'.//{{{NS}}}CodMotivo'), 'R02')
        self.assertEqual(seen[0].findtext(f'.//{{{NS}}}Righe/{{{NS}}}Riga/{{{NS}}}CodArt'), 'OT-001')
        self.assertEqual(seen[0].findtext(f'.//{{{NS}}}NumeroOrdine'), '000117')

    async def test_soap_timeout_missing_id_and_business_error_not_retried(self):
        for reply in ('<Envelope><Esito><Codice>OK</Codice></Esito></Envelope>', '<Envelope><Esito><Codice>ERR</Codice><CodErrore>POLICY</CodErrore></Esito></Envelope>', None):
            calls = []
            def respond(request):
                calls.append(request)
                if reply is None:
                    raise httpx.ReadTimeout('unit timeout')
                return httpx.Response(200, text=reply)
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                soap = OrdiNet(client, 'unit', 'unit', writes_enabled=True)
                soap._login = AsyncMock()
                with self.assertRaises(LegacyError):
                    await soap.create_return(NUMBER, [{'product_id': 'OT-001', 'quantity': 1}], 'other')
            self.assertEqual(len(calls), 1)
