"""Configuration, identity and output isolation tests; no serial connection."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import server

# Deliberately synthetic, never obtained from a physical printer.
FAKE_UUID = '00000000-0000-4000-8000-000000000000'
IDENTITY = ('MACHINE_TYPE:Ender-3 S1 EXTRUDER_COUNT:1 UUID:' + FAKE_UUID +
            '\nCap:AUTOLEVEL:1\nCap:Z_PROBE:1\nok')


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        self.patch = patch.object(server, 'ROOT', self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_configuration_is_required_before_any_serial_access(self):
        controller = server.Controller()
        try:
            with patch.object(server.list_ports, 'comports') as ports:
                with self.assertRaisesRegex(RuntimeError, 'printer.local.json'):
                    controller.prepare()
                ports.assert_not_called()
        finally:
            controller.logfile.close()

    def test_local_config_is_loaded_without_embedded_device_defaults(self):
        self.assertEqual(server.load_settings(), {})
        config = {'expected_uuid': FAKE_UUID, 'serial_port': None}
        (self.root / 'printer.local.json').write_text(json.dumps(config))
        self.assertEqual(server.load_settings(), config)

    def test_invalid_config_keys_or_types_are_rejected(self):
        for value in ([], {'expected_uuid': 3}, {'serial_port': []}, {'arbitrary_command': 'G28'}):
            (self.root / 'printer.local.json').write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                server.load_settings()

    def test_identity_match_preserves_model_and_probe_guards(self):
        server.verify_identity(IDENTITY, FAKE_UUID)
        server.verify_identity(IDENTITY.replace('Ender-3 S1 ', 'Ender-3 S1 Pro '), FAKE_UUID)
        for text in (IDENTITY.replace(FAKE_UUID, 'wrong'),
                     IDENTITY.replace('Ender-3 S1', 'Another Printer'),
                     IDENTITY.replace('Cap:Z_PROBE:1', 'Cap:Z_PROBE:0'),
                     IDENTITY.replace('Cap:AUTOLEVEL:1', 'Cap:AUTOLEVEL:0')):
            with self.assertRaises(RuntimeError):
                server.verify_identity(text, FAKE_UUID)

    def test_logs_redact_uuid_and_live_data_stays_in_local_data(self):
        # A sibling capture must not be silently imported by the public app.
        (self.root.parent / 'fresh-mesh-20990101.json').write_text('{"private":true}')
        controller = server.Controller()
        try:
            self.assertEqual(controller.data_dir, self.root / '.local-data')
            self.assertIsNone(controller.snapshot()['mesh'])
            controller.log('Firmware UUID:' + FAKE_UUID)
            self.assertNotIn(FAKE_UUID, controller.snapshot()['log'][-1])
            self.assertIn('UUID:[redacted]', (controller.data_dir / 'printer.log').read_text())
        finally:
            controller.logfile.close()


if __name__ == '__main__':
    unittest.main()
