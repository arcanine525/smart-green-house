"""Pure rejection tests for native recovery archives; no database/network calls."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from greenhouse.clickhouse.encoding import digest
from native_state import read_archive


class ArchiveGuardTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'archive.json'
        rows = [dict(kind='tariff', scope_id='fixture', version_id='v1', request_id='r1',
            schema_version=1, payload='{"value":"fixture"}', content_hash=digest({'value':'fixture'}))]
        self.archive = dict(format='greenhouse-native-rebuild-v1', backend='clickhouse', corrected_source_rows=0,
            configurations=dict(format='greenhouse-config-v1', rows=rows, checksum=digest(rows)))

    def write(self, archive, checksum=None):
        self.path.write_text(json.dumps(dict(archive=archive, sha256=checksum or digest(archive))))

    def test_intact_native_archive_is_readable(self):
        self.write(self.archive)
        archive, checksum = read_archive(self.path)
        self.assertEqual((archive, checksum), (self.archive, digest(self.archive)))

    def test_outer_checksum_and_backend_cannot_be_relabelled(self):
        self.write(self.archive, '0'*64)
        with self.assertRaisesRegex(ValueError, 'Invalid native'): read_archive(self.path)
        self.archive['backend'] = 'postgres'
        self.write(self.archive)
        with self.assertRaisesRegex(ValueError, 'Invalid native'): read_archive(self.path)

    def test_corrections_require_an_explicit_supported_replay(self):
        self.archive['corrected_source_rows'] = 1
        self.write(self.archive)
        with self.assertRaisesRegex(ValueError, 'uncorrected'): read_archive(self.path)

    def test_unknown_kind_is_rejected_even_with_valid_outer_checksums(self):
        self.archive['configurations']['rows'][0]['kind'] = 'future_unsupported'
        self.archive['configurations']['checksum'] = digest(self.archive['configurations']['rows'])
        self.write(self.archive)
        with self.assertRaisesRegex(ValueError, 'Unsupported configuration'): read_archive(self.path)

    def test_inner_payload_or_schema_corruption_is_rejected(self):
        for field, value in (('payload', '{"value":"changed"}'), ('schema_version', 99)):
            with self.subTest(field=field):
                archive = copy.deepcopy(self.archive)
                archive['configurations']['rows'][0][field] = value
                archive['configurations']['checksum'] = digest(archive['configurations']['rows'])
                self.write(archive)
                with self.assertRaisesRegex(ValueError, 'Corrupt/unsupported'): read_archive(self.path)


if __name__ == '__main__':
    unittest.main(verbosity=2)
