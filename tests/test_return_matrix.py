"""Finite state/input/interleaving matrix. No network: sender is forbidden in preview."""
import asyncio
import itertools
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from returns import ReturnRequest, ReturnService
from test_returns import EMAIL, KEY, NOW, NUMBER, FakeOrder, MemoryCAS, b64
from tools import ToolError

OPS = ('options', 'prepare', 'approve', 'cancel', 'status')
STATES = ('absent', 'prepared', 'expired', 'cancelled', 'submitting', 'submitted', 'submission_unknown', 'unsupported')
SCOPE = {'order_number': NUMBER, 'email': EMAIL}
LINES = [{'product_id': 'OT-001', 'quantity': 1}]


def request(message, mid, visitor='unit-visitor'):
    return SimpleNamespace(headers={'X-Chat-User-Ref': visitor, 'X-Chat-Message-Id': mid, 'X-Chat-User-Message-B64': b64(message)})


class Harness:
    def __init__(self):
        self.now = NOW
        self.store = MemoryCAS()
        self.order = FakeOrder()
        self.order.create_return = AsyncMock(side_effect=AssertionError('Preview must NEVER call a sender'))
        self.service = ReturnService(self.store, KEY, NUMBER, EMAIL, clock=lambda: self.now)
        self.last_draft = '0' * 32

    def body(self, op, reason='damaged', email=EMAIL):
        args = {**SCOPE, 'email': email, 'operation': op}
        if op == 'prepare':
            args.update(lines=LINES, reason=reason)
        if op in ('approve', 'cancel'):
            args['draft_id'] = self.last_draft
        return ReturnRequest(**args)

    async def call(self, op, mid, message=None, reason='damaged', visitor='unit-visitor', email=EMAIL):
        message = message if message is not None else {'approve': 'CONFERMO', 'cancel': 'ANNULLA'}.get(op, 'Una nuova richiesta')
        req = request(message, mid, visitor)
        body = self.body(op, reason, email)
        self.service.event(req)
        method = {'options': 'options', 'prepare': 'prepare', 'approve': 'decide', 'cancel': 'decide', 'status': 'check_status'}[op]
        result = await getattr(self.service, method)(req, body, self.order)
        if getattr(result, 'draft_id', None):
            self.last_draft = result.draft_id
        return result

    async def seed(self, state):
        if state == 'absent':
            return
        result = await self.call('prepare', 'seed')
        self.last_draft = result.draft_id
        if state == 'cancelled':
            await self.call('cancel', 'cancel-seed')
        elif state == 'expired':
            self.now += timedelta(minutes=16)
        elif state not in ('prepared',):
            # Unit-only fixtures, not real storage resets. No sender ever runs.
            data, etag = self.store.get('order-000117')
            data['state'] = 'legacy_unknown' if state == 'unsupported' else state
            if state == 'submitted':
                data['return_id'] = 'RMA-unit-existing'
            self.store.replace('order-000117', data, etag)

    def assert_safe(self, test):
        self.order.create_return.assert_not_called()
        row = self.store.get('order-000117')
        if row:
            test.assertIn(row[0]['state'], ('prepared', 'cancelled', 'submitting', 'submitted', 'submission_unknown', 'legacy_unknown'))
            for historic in row[0].get('cancelled_history', []):
                test.assertEqual(historic['state'], 'cancelled')
                test.assertNotEqual(historic['draft_id'], row[0]['draft_id'])


class ReturnMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_states_operations_messages_and_owners(self):
        messages = ('Una nuova richiesta', 'CONFERMO', 'ANNULLA', 'confermo', ' CONFERMO', 'CONFERMO\n', 'annulla voglio cambiare motivo', 'è rotto 🎁')
        count = 0
        for state, op, message, visitor in itertools.product(STATES, OPS, messages, ('unit-visitor', 'other-visitor')):
            with self.subTest(state=state, op=op, message=message, visitor=visitor):
                h = Harness()
                await h.seed(state)
                before = h.store.get('order-000117')
                try:
                    result = await h.call(op, 'matrix-new', message, visitor=visitor)
                    if op == 'approve' and result.submission_stopped:
                        self.assertEqual(result.state, 'prepared')
                        self.assertFalse(result.decision_recorded)
                except ToolError:
                    pass
                h.assert_safe(self)
                after = h.store.get('order-000117')
                if state in ('submitting', 'submitted', 'submission_unknown', 'unsupported'):
                    self.assertEqual(after, before)
                if op in ('options', 'status'):
                    self.assertEqual(after, before)
                count += 1
        print(f'Return input matrix: {count} state/operation/message/browser cases; zero sender calls.')

    async def test_all_three_step_sequences(self):
        count = 0
        for initial, sequence in itertools.product(('absent', 'prepared', 'cancelled', 'expired'), itertools.product(OPS, repeat=3)):
            with self.subTest(initial=initial, sequence=sequence):
                h = Harness()
                await h.seed(initial)
                for index, op in enumerate(sequence):
                    try:
                        await h.call(op, f'sequence-{index}', reason='damaged' if index % 2 else 'other')
                    except ToolError:
                        pass
                    h.assert_safe(self)
                count += 1
        print(f'Return sequence matrix: {count} three-step scenarios; zero sender calls.')

    async def test_all_concurrent_operation_pairs(self):
        count = 0
        for state, left, right in itertools.product(STATES, OPS, OPS):
            with self.subTest(state=state, left=left, right=right):
                h = Harness()
                await h.seed(state)
                # Separate requests represent separate user turns, with their own message IDs.
                results = await asyncio.gather(h.call(left, 'pair-a', reason='damaged'), h.call(right, 'pair-b', reason='other'), return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        self.assertIsInstance(result, ToolError)
                h.assert_safe(self)
                if left == right == 'cancel' and state == 'prepared':
                    self.assertEqual(sum(r.decision_recorded for r in results if not isinstance(r, Exception)), 1)
                count += 1
        print(f'Return concurrent matrix: {count} operation pairs; zero sender calls.')

    async def test_cross_browser_all_operations_recheck_actual_email(self):
        count = 0
        for state, op, visitor, email in itertools.product(STATES, OPS, ('phone', '', '{{unresolved}}'), (EMAIL, 'wrong@example.com')):
            h = Harness()
            # No fixture-email allowlist: exercise actual SOAP email verification.
            h.service = ReturnService(h.store, KEY, clock=lambda: h.now)
            await h.seed(state)
            before = h.store.get('order-000117')
            calls = h.order.calls
            try:
                await h.call(op, 'new-phone-message', visitor=visitor, email=email)
                self.assertEqual(email, EMAIL)
                if op in ('options', 'status'):
                    self.assertEqual(h.store.get('order-000117'), before)
            except ToolError as error:
                if email != EMAIL:
                    self.assertEqual(error.status, 404)
                    self.assertEqual(h.store.get('order-000117'), before)
            self.assertGreater(h.order.calls, calls)
            h.assert_safe(self)
            count += 1
        print(f'Cross-browser authorization matrix: {count} state/operation/browser/email cases; actual email rechecked, zero sender calls.')

    async def test_existing_visitor_bound_row_is_readable_without_rewrite(self):
        h = Harness()
        await h.seed('prepared')
        data, etag = h.store.get('order-000117')
        data['owner'] = h.service.digest('visitor:old-browser:email:' + EMAIL)
        h.store.replace('order-000117', data, etag)
        before = h.store.get('order-000117')
        for op in ('options', 'status'):
            result = await h.call(op, 'phone-read', visitor='phone')
            self.assertEqual(result.existing_state if op == 'options' else result.state, 'prepared')
        self.assertEqual(h.store.get('order-000117'), before)
        preview = await h.call('approve', 'new-phone-confirm', visitor='phone')
        self.assertTrue(preview.submission_stopped)
        self.assertEqual(preview.state, 'prepared')
        self.assertEqual(preview.draft_id, before[0]['draft_id'])
        self.assertEqual(h.store.get('order-000117')[0]['owner'], data['owner'])
        h.assert_safe(self)

    async def test_cross_browser_cancel_modify_and_renew(self):
        h = Harness()
        old = await h.call('prepare', 'desktop-prepare')
        h.now += timedelta(minutes=16)
        with self.assertRaises(ToolError):
            await h.call('approve', 'phone-expired', visitor='phone')
        renewed = await h.call('prepare', 'phone-renew', visitor='phone')
        self.assertNotEqual(renewed.draft_id, old.draft_id)
        changed = await h.call('prepare', 'other-phone-change', visitor='another-phone', reason='other')
        self.assertNotEqual(changed.draft_id, renewed.draft_id)
        cancelled = await h.call('cancel', 'desktop-cancel', visitor='desktop')
        self.assertEqual(cancelled.state, 'cancelled')
        fresh = await h.call('prepare', 'phone-new-request', visitor='phone')
        self.assertNotEqual(fresh.draft_id, cancelled.draft_id)
        self.assertEqual(h.store.get('order-000117')[0]['cancelled_history'][0]['draft_id'], cancelled.draft_id)
        h.assert_safe(self)

    async def test_request_schema_combinations(self):
        count = 0
        reasons = ('not_as_expected', 'damaged', 'wrong_item', 'changed_mind', 'other', 'invalid')
        for op, fields, quantity, reason, valid_draft in itertools.product(OPS, itertools.product((False, True), repeat=3), (0, 1, 2, True, '1', 1001), reasons, (False, True)):
            has_lines, has_reason, has_draft = fields
            args = {**SCOPE, 'operation': op}
            if has_lines:
                args['lines'] = [{'product_id': 'OT-001', 'quantity': quantity}]
            if has_reason:
                args['reason'] = reason
            if has_draft:
                args['draft_id'] = '1' * 32 if valid_draft else 'invalid'
            allowed = ((op == 'prepare' and has_lines and has_reason and not has_draft) or
                       (op in ('approve', 'cancel') and not has_lines and not has_reason and has_draft) or
                       (op in ('options', 'status') and not any(fields)))
            if has_lines:
                allowed = allowed and type(quantity) is int and 1 <= quantity <= 1000
            if has_reason:
                allowed = allowed and reason != 'invalid'
            if has_draft:
                allowed = allowed and valid_draft
            with self.subTest(args=args):
                if allowed:
                    ReturnRequest(**args)
                else:
                    with self.assertRaises(ValueError):
                        ReturnRequest(**args)
            count += 1
        for op, extra in itertools.product(OPS, ('confirmed', 'action', 'user_message', 'message_id', 'token', 'submission_enabled')):
            h = Harness()
            body = h.body(op).model_dump(exclude_none=True)
            body[extra] = True
            with self.assertRaises(ValueError):
                ReturnRequest(**body)
            count += 1
        print(f'Return schema matrix: {count} phase/field/quantity/reason/draft cases.')

    async def test_context_transport_combinations_before_any_write(self):
        count = 0
        encodings = (b64('Una richiesta'), b64('CONFERMO'), b64('ANNULLA'), b64('è rotto 🎁\n'), '', '{{unresolved}}', '/w==', '!bad!')
        for op, encoded, mid, visitor in itertools.product(OPS, encodings, ('fresh', '', '{{unresolved}}', 'bad space'), ('unit-visitor', '', '{{unresolved}}', 'bad space')):
            h = Harness()
            req = request('placeholder', mid, visitor)
            req.headers['X-Chat-User-Message-B64'] = encoded
            method = {'options': 'options', 'prepare': 'prepare', 'approve': 'decide', 'cancel': 'decide', 'status': 'check_status'}[op]
            try:
                h.service.event(req)
                await getattr(h.service, method)(req, h.body(op), h.order)
            except ToolError:
                pass
            h.assert_safe(self)
            if not mid or '{{' in mid or ' ' in mid or encoded in ('', '{{unresolved}}', '/w==', '!bad!'):
                self.assertEqual(h.store.rows, {})
            count += 1
        print(f'Return context matrix: {count} operation/encoding/message-ID/visitor cases; zero sender calls.')

    async def test_unknown_storage_ack_never_triggers_another_sender(self):
        h = Harness()
        await h.seed('prepared')
        original = h.store.replace
        def lose_ack(partition, data, etag):
            result = original(partition, data, etag)
            if result:
                raise RuntimeError('Write applied, acknowledgement lost')
            return result
        h.store.replace = lose_ack
        with self.assertRaises(ToolError):
            await h.call('cancel', 'cancel-ack-lost')
        self.assertEqual(h.store.get('order-000117')[0]['state'], 'cancelled')
        replay = await h.call('cancel', 'cancel-ack-lost')
        self.assertFalse(replay.decision_recorded)
        h.assert_safe(self)

    async def test_history_limit_fails_without_deleting_records(self):
        h = Harness()
        await h.seed('cancelled')
        data, etag = h.store.get('order-000117')
        data['cancelled_history'] = [{'state': 'cancelled', 'summary': 'x' * 31000, 'draft_id': 'old-history'}]
        h.store.replace('order-000117', data, etag)
        before = h.store.get('order-000117')
        with self.assertRaises(ToolError):
            await h.call('prepare', 'new-request')
        self.assertEqual(h.store.get('order-000117'), before)
        h.assert_safe(self)

    async def test_change_reason_without_cancel_invalidates_old_id(self):
        h = Harness()
        old = await h.call('prepare', 'old', reason='not_as_expected')
        changed = await h.call('prepare', 'change', message='annulla voglio cambiare motivo perché è rotto', reason='damaged')
        self.assertNotEqual(old.draft_id, changed.draft_id)
        self.assertEqual(changed.reason, 'damaged')
        self.assertEqual(h.store.get('order-000117')[0].get('cancelled_history'), [])
        h.last_draft = old.draft_id
        with self.assertRaises(ToolError):
            await h.call('approve', 'old-confirm')
        h.assert_safe(self)

    async def test_cancel_then_new_turn_preserves_history_and_invalidates_old_id(self):
        h = Harness()
        old = await h.call('prepare', 'old')
        await h.call('cancel', 'cancel')
        for message in ('ANNULLA', 'CONFERMO'):
            with self.assertRaises(ToolError):
                await h.call('prepare', 'cancel', message=message)
        with self.assertRaises(ToolError):
            await h.call('prepare', 'cancel', message='new wording but SAME message ID')
        fresh = await h.call('prepare', 'new-turn', message='Voglio una nuova proposta, motivo danneggiato')
        self.assertNotEqual(fresh.draft_id, old.draft_id)
        saved = h.store.get('order-000117')[0]
        self.assertEqual(saved['cancelled_history'][0]['draft_id'], old.draft_id)
        self.assertEqual(saved['cancelled_history'][0]['decision_message_id'], 'cancel')
        h.last_draft = old.draft_id
        with self.assertRaises(ToolError):
            await h.call('approve', 'stale-confirm')
        h.assert_safe(self)

    async def test_status_rereads_after_slow_soap_and_sees_concurrent_cancel(self):
        h = Harness()
        await h.seed('prepared')
        started, resume = asyncio.Event(), asyncio.Event()
        original = h.order.order
        async def slow_order(number):
            if not started.is_set():
                started.set()
                await resume.wait()
            return await original(number)
        h.order.order = slow_order
        task = asyncio.create_task(h.call('status', 'read'))
        await started.wait()
        cancelled = await h.call('cancel', 'cancel')
        self.assertTrue(cancelled.decision_recorded)
        resume.set()
        status = await task
        self.assertEqual(status.state, 'cancelled')
        h.assert_safe(self)

    async def test_stale_prepare_cannot_overwrite_concurrent_cancel(self):
        h = Harness()
        await h.seed('prepared')
        started, resume = asyncio.Event(), asyncio.Event()
        original = h.order.order
        async def slow_order(number):
            if not started.is_set():
                started.set()
                await resume.wait()
            return await original(number)
        h.order.order = slow_order
        task = asyncio.create_task(h.call('prepare', 'earlier-change', reason='other'))
        await started.wait()
        await h.call('cancel', 'later-cancel')
        resume.set()
        with self.assertRaises(ToolError):
            await task
        self.assertEqual(h.store.get('order-000117')[0]['state'], 'cancelled')
        h.assert_safe(self)

    async def test_expiry_during_soap_read_blocks_confirmation(self):
        h = Harness()
        await h.seed('prepared')
        original = h.order.order
        async def late_order(number):
            result = await original(number)
            h.now += timedelta(minutes=16)
            return result
        h.order.order = late_order
        with self.assertRaises(ToolError):
            await h.call('approve', 'late-confirm')
        self.assertEqual(h.store.get('order-000117')[0]['state'], 'prepared')
        h.assert_safe(self)

    async def test_history_preserved_across_multiple_new_requests(self):
        h = Harness()
        historic_ids = []
        for n in range(5):
            prepared = await h.call('prepare', f'prepare-{n}')
            historic_ids.append(prepared.draft_id)
            await h.call('cancel', f'cancel-{n}')
        await h.call('prepare', 'last-new-request')
        history = h.store.get('order-000117')[0]['cancelled_history']
        self.assertEqual([row['draft_id'] for row in history], historic_ids)
        h.assert_safe(self)

    async def test_date_status_and_quantity_boundaries(self):
        count = 0
        for status, age, quantity in itertools.product(('ST01', 'ST03', 'ST05', 'ST07', 'ST09'), (-1, 0, 1, 30, 31), (1, 2, 3)):
            h = Harness()
            h.order.xml.find('StatoCod').text = status
            h.order.xml.find('DataConsegna').text = (NOW - timedelta(days=age)).strftime('%d/%m/%Y')
            body = ReturnRequest(**SCOPE, operation='prepare', lines=[{'product_id': 'OT-001', 'quantity': quantity}], reason='other')
            req = request('Prepara la proposta', 'boundary')
            allowed = status == 'ST05' and 0 <= age <= 30 and quantity <= 2
            if allowed:
                self.assertEqual((await h.service.prepare(req, body, h.order)).state, 'prepared')
            else:
                with self.assertRaises(ToolError):
                    await h.service.prepare(req, body, h.order)
                self.assertEqual(h.store.rows, {})
            h.assert_safe(self)
            count += 1
        print(f'Return eligibility matrix: {count} status/date/quantity cases; zero sender calls.')
