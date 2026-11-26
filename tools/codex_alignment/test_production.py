import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.codex_alignment import production
from tools.codex_alignment.evidence import atomic_json


class ProductionSnapshotTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.receipt = directory / 'production.json'
        atomic_json(self.receipt, {'schema': 0})
        self.owner = (1234, 5678)
        self.config = {
            'account_id': 1, 'group_id': 2, 'app_container': 'app', 'pg_container': 'postgres',
            'gateway_port': 8080, 'snapshot_secret': 'test-only', 'output': self.receipt,
            'monitor_uid': str(self.owner[0]), 'monitor_gid': str(self.owner[1]),
        }
        self.enterContext(patch.object(production, 'inspect', return_value={
            'Id': 'container', 'Image': 'image', 'Config': {}, 'HostConfig': {},
            'State': {'Running': True},
        }))
        self.enterContext(patch.object(production, 'query', return_value={
            'account': {'proxy_id': None, 'type': 'oauth', 'status': 'active', 'schedulable': True},
        }))
        connection = self.enterContext(patch.object(production.http.client, 'HTTPConnection'))
        connection.return_value.getresponse.return_value.status = 200

    def test_receipt_is_readable_by_monitor_at_publication(self):
        owner_changes = []
        replace = os.replace

        def remember_owner(fd, uid, gid):
            owner_changes.append((os.fstat(fd).st_ino, uid, gid))

        def check_owner_and_publish(src, dst):
            self.assertEqual(dst, self.receipt)
            self.assertEqual(owner_changes, [(Path(src).stat().st_ino, *self.owner)],
                             'set the monitor ownership on the receipt before publication')
            replace(src, dst)

        with patch.object(os, 'fchown', side_effect=remember_owner), \
             patch.object(os, 'replace', side_effect=check_owner_and_publish):
            receipt = production.snapshot(self.config)

        self.assertEqual(json.loads(self.receipt.read_text()), receipt)
        self.assertEqual(self.receipt.stat().st_mode & 0o777, 0o600)

    def test_failed_owner_change_keeps_the_previous_receipt(self):
        with patch.object(os, 'fchown', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                production.snapshot(self.config)

        self.assertEqual(json.loads(self.receipt.read_text()), {'schema': 0})
        self.assertEqual(list(self.receipt.parent.iterdir()), [self.receipt])
