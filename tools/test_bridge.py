"""Offline session-bridge tests. Fake browser replies; no real shop is modified."""
from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard
from lib import mayzing
from lib.browser_bridge import BrowserBridge, BridgeError

READY = {'ready': True, 'message': 'Ready'}


class BridgeTests(unittest.TestCase):
    def connected(self, enabled=False):
        broker = BrowserBridge()
        token = broker.pair(broker.pairing_code()['code'])['token']
        broker.heartbeat(token, READY)
        if enabled:
            broker.enable()
        return broker, token

    def request_thread(self, broker, *, timeout=1):
        outcome = {}
        def request():
            try:
                outcome['reply'] = broker.request('CloneGroup', {}, mutating=True, timeout=timeout)
            except BridgeError as exc:
                outcome['error'] = str(exc)
        thread = threading.Thread(target=request, daemon=True)
        thread.start()
        with broker.cv:
            self.assertTrue(broker.cv.wait_for(lambda: bool(broker.commands), timeout=1))
        return thread, outcome

    def test_pairing_code_is_one_use_and_status_contains_no_credentials(self):
        broker = BrowserBridge()
        code = broker.pairing_code()['code']
        token = broker.pair(code)['token']
        with self.assertRaises(BridgeError):
            broker.pair(code)
        status = json.dumps(broker.status())
        self.assertNotIn(token, status)
        self.assertNotIn(code, status)

    def test_pairing_code_is_valid_for_one_day(self):
        broker = BrowserBridge()
        with patch('lib.browser_bridge.time.monotonic', return_value=100):
            issued = broker.pairing_code()
        self.assertEqual(issued['expires_in'], 86400)
        with patch('lib.browser_bridge.time.monotonic', return_value=100 + 86400 - 1):
            self.assertTrue(broker.pair(issued['code'])['token'])

    def test_pairing_code_expires_at_one_day(self):
        broker = BrowserBridge()
        with patch('lib.browser_bridge.time.monotonic', return_value=100):
            issued = broker.pairing_code()
        with patch('lib.browser_bridge.time.monotonic', return_value=100 + 86400):
            with self.assertRaisesRegex(BridgeError, 'hết hạn'):
                broker.pair(issued['code'])

    def test_no_mutation_before_explicit_enable(self):
        broker, _ = self.connected()
        with self.assertRaisesRegex(BridgeError, 'Chưa bật'):
            broker.request('CloneGroup', {}, mutating=True)
        self.assertFalse(broker.commands)

    def test_enable_checks_authenticated_stores_before_permitting_upload(self):
        broker, token = self.connected()
        seen = []
        def browser():
            command = broker.poll(token, READY, wait_s=1)['command']
            seen.append(command['operation'])
            broker.acknowledge(token, command['id'], {'kind': 'response', 'status': 200,
                'text': json.dumps({'data': {'stores': [{'slug': 'TEST'}]}})})
        worker = threading.Thread(target=browser, daemon=True)
        worker.start()
        with patch.object(mayzing, 'BRIDGE', broker):
            result = mayzing.enable_automation()
        worker.join(timeout=2)
        self.assertTrue(result['ok'])
        self.assertEqual(seen, ['StoresMinimal'])
        self.assertTrue(broker.status()['automation'])

    def test_claimed_mutation_is_delivered_only_once(self):
        broker, token = self.connected(enabled=True)
        caller, outcome = self.request_thread(broker)
        command = broker.poll(token, READY, wait_s=0)['command']
        self.assertIsNone(broker.poll(token, READY, wait_s=0)['command'])
        reply = {'kind': 'response', 'status': 200, 'text': '{"data":{}}'}
        broker.acknowledge(token, command['id'], reply)
        caller.join(timeout=1)
        self.assertEqual(outcome['reply'], reply)
        broker.acknowledge(token, command['id'], reply)
        self.assertFalse(broker.commands)

    def test_uncertain_mutation_stops_without_retry(self):
        broker, token = self.connected(enabled=True)
        caller, outcome = self.request_thread(broker, timeout=0.2)
        broker.poll(token, READY, wait_s=0)
        caller.join(timeout=1)
        self.assertIn('không gửi lại', outcome['error'])
        self.assertFalse(broker.status()['automation'])
        self.assertIsNone(broker.poll(token, READY, wait_s=0)['command'])

    def test_disable_prevents_queued_writes_from_being_sent(self):
        broker, token = self.connected(enabled=True)
        caller, outcome = self.request_thread(broker)
        broker.disable()
        self.assertIsNone(broker.poll(token, READY, wait_s=0)['command'])
        caller.join(timeout=1)
        self.assertFalse(caller.is_alive())
        self.assertFalse(broker.status()['automation'])

    def test_disconnect_releases_waiting_requests(self):
        broker, _ = self.connected(enabled=True)
        caller, outcome = self.request_thread(broker)
        broker.disconnect()
        caller.join(timeout=1)
        self.assertIn('bị ngắt', outcome['error'])
        self.assertFalse(broker.commands)

    def test_completed_reply_cannot_cross_pairing_generation(self):
        broker, token = self.connected(enabled=True)
        caller, outcome = self.request_thread(broker)
        command = broker.poll(token, READY, wait_s=0)['command']
        # Keep the waiting caller asleep until its old reply has been completed
        # and a different browser has replaced that connection.
        with broker.cv:
            broker.acknowledge(token, command['id'], {
                'kind': 'response', 'status': 200, 'text': 'old browser reply'})
            replacement = broker.pair(broker.pairing_code()['code'])['token']
            broker.heartbeat(replacement, READY)
        caller.join(timeout=1)
        self.assertFalse(caller.is_alive())
        self.assertIn('error', outcome)
        self.assertNotIn('reply', outcome)
        self.assertFalse(broker.commands)

    def test_pairing_discards_commands_queued_for_previous_browser(self):
        broker, _ = self.connected(enabled=True)
        caller, outcome = self.request_thread(broker)
        with broker.cv:
            replacement = broker.pair(broker.pairing_code()['code'])['token']
            broker.heartbeat(replacement, READY)
            self.assertFalse(broker.commands)
            self.assertIsNone(broker.poll(replacement, READY, wait_s=0)['command'])
        caller.join(timeout=1)
        self.assertFalse(caller.is_alive())
        self.assertIn('error', outcome)
        self.assertNotIn('reply', outcome)

    def test_enable_rejects_verification_from_previous_browser(self):
        broker, _ = self.connected()
        verified_generation = broker.generation
        replacement = broker.pair(broker.pairing_code()['code'])['token']
        broker.heartbeat(replacement, READY)
        with self.assertRaises(BridgeError):
            broker.enable(expected_generation=verified_generation)
        self.assertFalse(broker.status()['automation'])
        broker.enable(expected_generation=broker.generation)
        self.assertTrue(broker.status()['automation'])

    def test_request_rejects_job_bound_to_previous_browser(self):
        broker, _ = self.connected(enabled=True)
        job_generation = broker.generation
        replacement = broker.pair(broker.pairing_code()['code'])['token']
        broker.heartbeat(replacement, READY)
        broker.enable()
        with self.assertRaises(BridgeError):
            broker.request('CloneGroup', {}, mutating=True, timeout=0.1,
                           expected_generation=job_generation)
        self.assertFalse(broker.commands)

    def test_lost_heartbeat_requires_enabling_again(self):
        broker, token = self.connected(enabled=True)
        broker.last_seen = time.monotonic() - 40
        self.assertFalse(broker.status()['automation'])
        broker.heartbeat(token, READY)
        self.assertTrue(broker.status()['connected'])
        self.assertFalse(broker.status()['automation'])

    def test_verification_pauses_transport(self):
        broker, _ = self.connected(enabled=True)
        with patch.object(mayzing, 'BRIDGE', broker):
            with self.assertRaises(mayzing.VerificationRequired):
                mayzing._decode_reply({'kind': 'verification', 'message': 'Verify manually'})
        self.assertFalse(broker.status()['automation'])

    def test_unknown_graphql_document_is_rejected(self):
        with self.assertRaises(mayzing.MayzingError):
            mayzing.Mayzing().gql('mutation CloneGroup { deleteAll }', op='CloneGroup')

    def test_websites_cannot_create_codes_or_enable_automation(self):
        client = dashboard.app.test_client()
        for path in ('/api/bridge/code', '/api/automation/enable', '/api/automation/disable', '/api/publish'):
            response = client.post(path, json={}, base_url='http://127.0.0.1:8780',
                                   headers={'Origin': 'https://untrusted.example'})
            self.assertEqual(response.status_code, 403)

    def test_extension_pairs_once_and_wrong_token_is_rejected(self):
        broker = BrowserBridge()
        client = dashboard.app.test_client()
        origin = 'chrome-extension://' + 'a' * 32
        with patch.object(dashboard, 'BRIDGE', broker):
            code = client.post('/api/bridge/code', json={}, base_url='http://127.0.0.1:8780').get_json()['code']
            response = client.post('/api/bridge/pair', json={'code': code},
                base_url='http://127.0.0.1:8780', headers={'Origin': origin})
            self.assertIn('token', response.get_json())
            response = client.post('/api/bridge/heartbeat', json={'page': READY},
                base_url='http://127.0.0.1:8780',
                headers={'Origin': origin, 'Authorization': 'Bearer wrong'})
            self.assertEqual(response.status_code, 401)

    def test_extension_has_no_cookie_or_debugger_permission(self):
        manifest = json.loads((Path(__file__).resolve().parents[1] / 'chrome-extension/manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(set(manifest['permissions']), {'activeTab', 'scripting', 'storage'})
        self.assertEqual(set(manifest['host_permissions']),
                         {'http://127.0.0.1/*', 'http://localhost/*', 'https://mz.eagleglobal2023.com/*'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
