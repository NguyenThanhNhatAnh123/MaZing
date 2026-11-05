"""Offline checks for normal Chrome launch and disabled browser automation."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import dashboard
from lib import mayzing


class NormalChromeTests(unittest.TestCase):
    def test_normal_launch_uses_only_the_executable_and_url(self):
        executable = str(Path(__file__).resolve())
        with patch.object(config, 'BROWSER_EXECUTABLE_PATH', executable), \
             patch.object(mayzing.subprocess, 'Popen') as launch:
            mayzing.open_chrome()
        self.assertEqual(launch.call_args.args[0],
                         [executable, config.MAYZING_DASHBOARD + '/dashboard'])

    def test_missing_chrome_does_not_start_another_browser(self):
        with patch.object(config, 'BROWSER_EXECUTABLE_PATH', 'C:/missing-chrome.exe'), \
             patch.object(mayzing.subprocess, 'Popen') as launch:
            with self.assertRaises(mayzing.MayzingError):
                mayzing.open_chrome()
        launch.assert_not_called()

    def test_open_endpoint_does_not_create_a_login_job(self):
        job = dashboard.Job()
        with patch.object(dashboard, 'JOB', job), patch.object(mayzing, 'open_chrome') as launch:
            response = dashboard.app.test_client().post('/api/login/start')
        self.assertTrue(response.get_json()['manual'])
        self.assertFalse(job.running)
        launch.assert_called_once_with()

    def test_session_checks_do_not_open_or_control_chrome(self):
        with patch.object(mayzing.subprocess, 'Popen') as launch:
            result = mayzing.check_session()
            self.assertFalse(result['ok'])
            self.assertFalse(mayzing.has_session_file())
        launch.assert_not_called()

    def test_automatic_upload_and_publish_are_blocked(self):
        with patch.object(mayzing, 'open_chrome') as launch:
            client = dashboard.app.test_client()
            self.assertEqual(client.post('/api/upload/start', json={}).status_code, 409)
            self.assertEqual(client.post('/api/publish', json={}).status_code, 409)
            with self.assertRaisesRegex(mayzing.MayzingError, 'Bật tự động'):
                with mayzing.Mayzing():
                    self.fail('Automatic transport must not start')
        launch.assert_not_called()

    def test_only_one_dashboard_can_own_a_port(self):
        with tempfile.TemporaryDirectory(prefix='mayzing_lock_test_') as folder, \
             patch.object(config, 'WORK_DIR', Path(folder)):
            first = dashboard._acquire_dashboard_lock(8780)
            try:
                self.assertIsNotNone(first)
                self.assertIsNone(dashboard._acquire_dashboard_lock(8780))
            finally:
                first.close()
            reopened = dashboard._acquire_dashboard_lock(8780)
            self.assertIsNotNone(reopened)
            reopened.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
