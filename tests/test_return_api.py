import unittest
from unittest.mock import patch

import httpx
from main import app
from returns import ReturnService
from test_returns import EMAIL, KEY, NOW, NUMBER, FakeOrder, MemoryCAS, b64


class ReturnApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict('os.environ', {'TOOL_API_KEY': 'unit-key'})
        self.env.start()
        app.state.api_key = 'unit-key'
        self.store, self.order = MemoryCAS(), FakeOrder()
        app.state.returns = ReturnService(self.store, KEY, NUMBER, EMAIL, clock=lambda: NOW, submission_enabled=True)
        app.state.ordinet = self.order
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://unit')
        self.headers = {'X-API-Key': 'unit-key', 'X-Chat-User-Ref': 'visitor', 'X-Chat-Message-Id': 'prepare', 'X-Chat-User-Message-B64': b64('Vorrei un reso')}
        self.scope = {'order_number': NUMBER, 'email': EMAIL}
        self.prepare = {**self.scope, 'operation': 'prepare', 'lines': [{'product_id': 'OT-001', 'quantity': 1}], 'reason': 'damaged'}

    async def asyncTearDown(self):
        await self.client.aclose()
        self.env.stop()

    async def call(self, body):
        return await self.client.post('/returns', headers=self.headers, json=body)

    async def test_full_http_cycle_and_replay(self):
        options = await self.call({**self.scope, 'operation': 'options'})
        self.assertTrue(options.json()['eligible'])
        draft = (await self.call(self.prepare)).json()
        self.assertEqual(draft['state'], 'prepared')
        self.headers.update({'X-Chat-Message-Id': 'decision', 'X-Chat-User-Message-B64': b64('CONFERMO')})
        body = {**self.scope, 'operation': 'approve', 'draft_id': draft['draft_id']}
        result = (await self.call(body)).json()
        self.assertEqual(result['return_id'], 'RMA-test-001')
        self.assertTrue(result['decision_recorded'])
        self.assertFalse((await self.call(body)).json()['decision_recorded'])
        self.assertEqual((await self.call(self.prepare)).status_code, 422)
        status = (await self.call({**self.scope, 'operation': 'status'})).json()
        self.assertEqual(status['state'], 'submitted')
        self.assertFalse(status['decision_recorded'])
        self.order.create_return.assert_awaited_once()

    async def test_status_without_proposal_is_not_a_verification_error(self):
        options = await self.call({**self.scope, 'operation': 'options'})
        self.assertTrue(options.json()['eligible'])
        self.assertIsNone(options.json()['existing_state'])
        result = await self.call({**self.scope, 'operation': 'status'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['state'], 'not_prepared')
        self.assertIsNone(result.json()['draft_id'])
        self.assertFalse(result.json()['decision_recorded'])
        self.assertEqual(self.store.rows, {})
        self.order.create_return.assert_not_called()
        prepared = await self.call(self.prepare)
        self.assertEqual(prepared.status_code, 200)
        self.assertEqual(prepared.json()['state'], 'prepared')

    async def test_wrong_email_status_without_proposal_still_rejected(self):
        app.state.returns = ReturnService(self.store, KEY, clock=lambda: NOW)
        result = await self.call({**self.scope, 'email': 'wrong@example.com', 'operation': 'status'})
        self.assertEqual(result.status_code, 404)
        self.assertEqual(self.store.rows, {})

    async def test_status_from_another_browser_or_no_visitor(self):
        draft = (await self.call(self.prepare)).json()
        before = self.store.get('order-000117')
        for visitor in ('another-visitor', '{{unresolved}}', None):
            if visitor is None:
                self.headers.pop('X-Chat-User-Ref', None)
            else:
                self.headers['X-Chat-User-Ref'] = visitor
            result = await self.call({**self.scope, 'operation': 'status'})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()['draft_id'], draft['draft_id'])
            wrong = await self.call({**self.scope, 'email': 'wrong@example.com', 'operation': 'status'})
            self.assertEqual(wrong.status_code, 404)
        self.assertEqual(self.store.get('order-000117'), before)
        self.order.create_return.assert_not_called()

    async def test_preview_confirmation_keeps_open_proposal(self):
        app.state.returns.submission_enabled = False
        draft = (await self.call(self.prepare)).json()
        self.assertFalse(draft['submission_enabled'])
        self.headers.update({'X-Chat-Message-Id': 'decision', 'X-Chat-User-Message-B64': b64('CONFERMO')})
        result = await self.call({**self.scope, 'operation': 'approve', 'draft_id': draft['draft_id']})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()['submission_stopped'])
        self.assertEqual(result.json()['state'], 'prepared')
        self.assertFalse(result.json()['decision_recorded'])
        self.order.create_return.assert_not_called()
        self.assertEqual((await self.call({**self.scope, 'operation': 'status'})).json()['state'], 'prepared')

    async def test_cancel(self):
        draft = (await self.call(self.prepare)).json()
        self.headers.update({'X-Chat-Message-Id': 'decision', 'X-Chat-User-Message-B64': b64('ANNULLA')})
        result = await self.call({**self.scope, 'operation': 'cancel', 'draft_id': draft['draft_id']})
        self.assertEqual(result.json()['state'], 'cancelled')
        self.order.create_return.assert_not_called()

    async def test_invalid_input_and_transport_before_reads(self):
        invalid = [{**self.scope, 'operation': 'prepare'}, {**self.prepare, 'confirmed': True}, {**self.scope, 'operation': 'approve'}, {**self.scope, 'operation': 'status', 'reason': 'other'}, {'order_number': NUMBER, 'operation': 'options'}]
        for body in invalid:
            self.assertEqual((await self.call(body)).status_code, 422)
        self.headers['X-Chat-User-Message-B64'] = '{{unresolved}}'
        self.assertEqual((await self.call({**self.scope, 'operation': 'options'})).status_code, 422)
        self.assertEqual(self.order.calls, 0)
        self.order.create_return.assert_not_called()

    async def test_auth_and_schema(self):
        self.headers.pop('X-API-Key')
        self.assertEqual((await self.call(self.prepare)).status_code, 401)
        schema = (await self.client.get('/openapi.json')).json()
        self.assertEqual(schema['paths']['/returns']['post']['operationId'], 'manage_return')
        self.assertNotIn('confirmed', schema['components']['schemas']['ReturnRequest']['properties'])
        self.assertNotIn('simulated', schema['components']['schemas']['ReturnStatus']['properties'])
