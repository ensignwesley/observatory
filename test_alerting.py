#!/usr/bin/env python3
"""
Tests for Observatory alerting state machine.

Tests cover:
  - update_alert_state: all four state-machine branches
  - alert threshold: N consecutive failures required before DOWN fires
  - anti-spam: DOWN→DOWN does not re-alert
  - recovery: DOWN→UP fires exactly once
  - dispatch_alert: correct message text for DOWN and UP events
  - load_alert_config: missing file, enabled=false, valid config
  - compute_anomaly: z-score calculation and anomaly threshold

Run: python3 test_alerting.py
"""

import json
import math
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import checker

# ── Helpers ───────────────────────────────────────────────────────────────────

MOCK_TGT = {
    'slug':        'test-svc',
    'name':        'Test Service',
    'link':        'https://example.com/test',
    'url':         'http://127.0.0.1:9999/test',
    'threshold_ms': 300,
}

def make_db():
    """Return an in-memory SQLite connection with the full schema applied."""
    conn = sqlite3.connect(':memory:')
    conn.executescript(checker.SCHEMA)
    conn.commit()
    return conn


def get_state(conn, slug):
    row = conn.execute(
        "SELECT state, consecutive_failures, last_alerted_at, last_state_change_at "
        "FROM alert_state WHERE slug=?", (slug,)
    ).fetchone()
    if row is None:
        return None
    return {'state': row[0], 'consec': row[1],
            'last_alerted_at': row[2], 'last_state_change_at': row[3]}


# ── State machine tests ───────────────────────────────────────────────────────

class TestStateMachineNoAlerts(unittest.TestCase):
    """State transitions with alerting disabled (alert_cfg=None)."""

    def setUp(self):
        self.conn = make_db()
        self.now  = int(time.time())

    def tearDown(self):
        self.conn.close()

    def _step(self, ok):
        checker.update_alert_state(self.conn, MOCK_TGT, ok, None, self.now)
        self.now += 300   # advance 5 min

    def test_initial_state_seeded_on_first_call(self):
        self._step(True)
        st = get_state(self.conn, MOCK_TGT['slug'])
        self.assertIsNotNone(st)
        self.assertEqual(st['state'], 'UP')

    def test_consecutive_success_stays_up(self):
        for _ in range(5):
            self._step(True)
        st = get_state(self.conn, MOCK_TGT['slug'])
        self.assertEqual(st['state'], 'UP')
        self.assertEqual(st['consec'], 0)

    def test_single_failure_does_not_flip_to_down(self):
        self._step(True)
        self._step(False)         # one failure
        st = get_state(self.conn, MOCK_TGT['slug'])
        self.assertEqual(st['state'], 'UP')
        self.assertEqual(st['consec'], 1)

    def test_threshold_minus_one_failures_still_up(self):
        """ALERT_THRESHOLD-1 consecutive failures must not flip state."""
        n = checker.ALERT_THRESHOLD - 1
        self._step(True)
        for _ in range(n):
            self._step(False)
        st = get_state(self.conn, MOCK_TGT['slug'])
        self.assertEqual(st['state'], 'UP')
        self.assertEqual(st['consec'], n)

    def test_threshold_failures_flip_to_down(self):
        self._step(True)
        for _ in range(checker.ALERT_THRESHOLD):
            self._step(False)
        st = get_state(self.conn, MOCK_TGT['slug'])
        self.assertEqual(st['state'], 'DOWN')

    def test_recovery_after_down_flips_to_up(self):
        self._step(True)
        for _ in range(checker.ALERT_THRESHOLD):
            self._step(False)
        self.assertEqual(get_state(self.conn, MOCK_TGT['slug'])['state'], 'DOWN')
        self._step(True)
        st = get_state(self.conn, MOCK_TGT['slug'])
        self.assertEqual(st['state'], 'UP')
        self.assertEqual(st['consec'], 0)

    def test_failure_then_recovery_before_threshold_resets_counter(self):
        """Single failure followed by success: counter resets, state stays UP."""
        self._step(True)
        self._step(False)   # consec = 1
        self._step(True)    # recovery before threshold
        st = get_state(self.conn, MOCK_TGT['slug'])
        self.assertEqual(st['state'], 'UP')
        self.assertEqual(st['consec'], 0)

    def test_down_state_increments_consec_without_re_alerting(self):
        """While DOWN, consecutive_failures keeps incrementing (for dashboards)."""
        self._step(True)
        for _ in range(checker.ALERT_THRESHOLD):
            self._step(False)
        consec_at_flip = get_state(self.conn, MOCK_TGT['slug'])['consec']
        # Keep failing
        for _ in range(3):
            self._step(False)
        st = get_state(self.conn, MOCK_TGT['slug'])
        self.assertEqual(st['state'], 'DOWN')
        self.assertGreater(st['consec'], consec_at_flip)


class TestStateMachineAlertFires(unittest.TestCase):
    """Verify dispatch_alert is called at the right moments."""

    def setUp(self):
        self.conn    = make_db()
        self.now     = int(time.time())
        self.cfg     = {'threshold': checker.ALERT_THRESHOLD, 'channels': {}}

    def tearDown(self):
        self.conn.close()

    def _step(self, ok):
        checker.update_alert_state(self.conn, MOCK_TGT, ok, self.cfg, self.now)
        self.now += 300

    @patch('checker.dispatch_alert')
    def test_down_alert_fires_exactly_once_at_threshold(self, mock_dispatch):
        self._step(True)
        for _ in range(checker.ALERT_THRESHOLD):
            self._step(False)
        mock_dispatch.assert_called_once()
        args = mock_dispatch.call_args
        self.assertEqual(args[0][2], 'DOWN')    # new_state

    @patch('checker.dispatch_alert')
    def test_no_alert_before_threshold(self, mock_dispatch):
        self._step(True)
        for _ in range(checker.ALERT_THRESHOLD - 1):
            self._step(False)
        mock_dispatch.assert_not_called()

    @patch('checker.dispatch_alert')
    def test_no_re_alert_while_down(self, mock_dispatch):
        """Continued failures after DOWN must not fire additional alerts."""
        self._step(True)
        for _ in range(checker.ALERT_THRESHOLD + 5):
            self._step(False)
        # Should have been called exactly once (the first DOWN transition)
        self.assertEqual(mock_dispatch.call_count, 1)

    @patch('checker.dispatch_alert')
    def test_recovery_alert_fires_once(self, mock_dispatch):
        self._step(True)
        for _ in range(checker.ALERT_THRESHOLD):
            self._step(False)
        mock_dispatch.reset_mock()
        self._step(True)    # recovery
        mock_dispatch.assert_called_once()
        args = mock_dispatch.call_args
        self.assertEqual(args[0][2], 'UP')

    @patch('checker.dispatch_alert')
    def test_full_cycle_two_alerts_total(self, mock_dispatch):
        """Down then recover = exactly 2 alerts (1 DOWN + 1 UP)."""
        self._step(True)
        for _ in range(checker.ALERT_THRESHOLD):
            self._step(False)
        self._step(True)   # recovery
        self.assertEqual(mock_dispatch.call_count, 2)
        calls = [c[0][2] for c in mock_dispatch.call_args_list]
        self.assertEqual(calls, ['DOWN', 'UP'])

    @patch('checker.dispatch_alert')
    def test_flap_two_cycles_four_alerts(self, mock_dispatch):
        """Two full DOWN/UP cycles = 4 alerts total."""
        for _ in range(2):
            self._step(True)
            for _ in range(checker.ALERT_THRESHOLD):
                self._step(False)
            self._step(True)   # recovery
        self.assertEqual(mock_dispatch.call_count, 4)


# ── dispatch_alert message text ───────────────────────────────────────────────

class TestDispatchAlertText(unittest.TestCase):
    """Verify the message text sent to channels."""

    def setUp(self):
        self.cfg = {'channels': {'telegram': {'token': 'T', 'chat_id': 'C'}}}

    @patch('checker._send_telegram')
    def test_down_message_contains_service_name(self, mock_tg):
        checker.dispatch_alert(self.cfg, MOCK_TGT, 'DOWN', 2, None)
        text = mock_tg.call_args[0][2]
        self.assertIn('Test Service', text)
        self.assertIn('DOWN', text)
        self.assertIn('🔴', text)

    @patch('checker._send_telegram')
    def test_up_message_contains_service_name(self, mock_tg):
        down_since = time.time() - 600    # 10 min ago
        checker.dispatch_alert(self.cfg, MOCK_TGT, 'UP', 0, down_since)
        text = mock_tg.call_args[0][2]
        self.assertIn('Test Service', text)
        self.assertIn('UP', text)
        self.assertIn('🟢', text)
        self.assertIn('10 min', text)

    @patch('checker._send_telegram')
    def test_up_message_without_down_since(self, mock_tg):
        checker.dispatch_alert(self.cfg, MOCK_TGT, 'UP', 0, None)
        text = mock_tg.call_args[0][2]
        self.assertIn('🟢', text)

    @patch('checker._send_telegram')
    @patch('checker._send_webhook')
    def test_both_channels_called(self, mock_wh, mock_tg):
        cfg = {'channels': {
            'telegram': {'token': 'T', 'chat_id': 'C'},
            'webhook':  {'url': 'https://wh.example.com/', 'method': 'POST'},
        }}
        checker.dispatch_alert(cfg, MOCK_TGT, 'DOWN', 2, None)
        mock_tg.assert_called_once()
        mock_wh.assert_called_once()

    @patch('checker._send_telegram')
    def test_no_telegram_if_no_token(self, mock_tg):
        cfg = {'channels': {'telegram': {'token': '', 'chat_id': 'C'}}}
        checker.dispatch_alert(cfg, MOCK_TGT, 'DOWN', 2, None)
        mock_tg.assert_not_called()


# ── load_alert_config ─────────────────────────────────────────────────────────

class TestLoadAlertConfig(unittest.TestCase):

    def test_returns_none_when_file_missing(self):
        orig = checker.ALERT_CONFIG_PATH
        checker.ALERT_CONFIG_PATH = Path('/tmp/this-file-does-not-exist-observatory.json')
        try:
            result = checker.load_alert_config()
            self.assertIsNone(result)
        finally:
            checker.ALERT_CONFIG_PATH = orig

    def test_returns_none_when_enabled_false(self):
        cfg = {'alerting': {'enabled': False, 'channels': {}}}
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(cfg, f)
            tmp = Path(f.name)
        orig = checker.ALERT_CONFIG_PATH
        checker.ALERT_CONFIG_PATH = tmp
        try:
            result = checker.load_alert_config()
            self.assertIsNone(result)
        finally:
            checker.ALERT_CONFIG_PATH = orig
            tmp.unlink()

    def test_returns_config_when_enabled_true(self):
        cfg = {'alerting': {'enabled': True, 'threshold': 3, 'channels': {}}}
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(cfg, f)
            tmp = Path(f.name)
        orig = checker.ALERT_CONFIG_PATH
        checker.ALERT_CONFIG_PATH = tmp
        try:
            result = checker.load_alert_config()
            self.assertIsNotNone(result)
            self.assertEqual(result['threshold'], 3)
        finally:
            checker.ALERT_CONFIG_PATH = orig
            tmp.unlink()

    def test_returns_none_on_invalid_json(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write('not valid json {{{')
            tmp = Path(f.name)
        orig = checker.ALERT_CONFIG_PATH
        checker.ALERT_CONFIG_PATH = tmp
        try:
            result = checker.load_alert_config()
            self.assertIsNone(result)
        finally:
            checker.ALERT_CONFIG_PATH = orig
            tmp.unlink()


# ── compute_anomaly ───────────────────────────────────────────────────────────

class TestComputeAnomaly(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.now  = int(time.time())
        self.slug = 'anomaly-test'

    def tearDown(self):
        self.conn.close()

    def _insert(self, ms, offset_s=0):
        ts = self.now - offset_s
        self.conn.execute(
            "INSERT INTO checks (ts,target,url,ok,status_code,response_ms,zscore,anomaly) "
            "VALUES (?,?,?,1,200,?,NULL,0)",
            (ts, self.slug, 'http://x', ms)
        )
        self.conn.commit()

    def test_fewer_than_min_samples_returns_none(self):
        for i in range(checker.ANOMALY_MIN_SAMP - 1):
            self._insert(100.0, offset_s=i*60)
        z, anomaly = checker.compute_anomaly(self.conn, self.slug, self.now, 100.0)
        self.assertIsNone(z)
        self.assertEqual(anomaly, 0)

    def test_normal_value_not_anomaly(self):
        for i in range(20):
            self._insert(100.0, offset_s=i*60)
        z, anomaly = checker.compute_anomaly(self.conn, self.slug, self.now, 102.0)
        self.assertEqual(anomaly, 0)

    def test_spike_is_anomaly(self):
        # Baseline: values around 100ms with some variance (so std > 0)
        # Use alternating 90/110 → mean=100, std=10
        for i in range(20):
            self._insert(90.0 if i % 2 == 0 else 110.0, offset_s=i*60)
        # 900ms is (900-100)/10 = 80 standard deviations out — definitely anomaly
        z, anomaly = checker.compute_anomaly(self.conn, self.slug, self.now, 900.0)
        self.assertEqual(anomaly, 1)
        self.assertGreater(z, checker.ANOMALY_Z)

    def test_zero_std_returns_zero_zscore(self):
        for i in range(20):
            self._insert(100.0, offset_s=i*60)
        z, anomaly = checker.compute_anomaly(self.conn, self.slug, self.now, 100.0)
        self.assertEqual(z, 0.0)
        self.assertEqual(anomaly, 0)

    def test_old_samples_outside_window_excluded(self):
        # Insert samples just outside the window — should not count
        for i in range(20):
            self._insert(100.0, offset_s=checker.ANOMALY_WINDOW_S + 60 + i*60)
        # With no recent samples, should return None (< ANOMALY_MIN_SAMP)
        z, anomaly = checker.compute_anomaly(self.conn, self.slug, self.now, 100.0)
        self.assertIsNone(z)


if __name__ == '__main__':
    unittest.main(verbosity=2)
