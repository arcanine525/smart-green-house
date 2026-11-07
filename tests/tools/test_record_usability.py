"""Synthetic terminal input verifies recording mechanics, not human results."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class RecordingTest(unittest.TestCase):
    def test_six_observations_and_no_overwrite(self):
        script = Path(__file__).resolve().parents[2] / 'scripts' / 'record_usability.py'
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'simulated.json'
            answers = ['thanh_vien_gia_lap', 'fixture role', 'test-version', 'fixture context']
            for _ in range(6):
                answers += ['', '', 'khong_tro_giup', 'fixture answer', 'dung',
                            'fixture explanation', 'dung', 'fixture feedback']
            result = subprocess.run([sys.executable, str(script), '--participant', 'test-P01',
                                     '--output', str(output)], input='\n'.join(answers) + '\n',
                                    text=True, capture_output=True, check=True)
            self.assertIn('Đã lưu', result.stdout)
            payload = json.loads(output.read_text())
            self.assertEqual([row['br'] for row in payload['tasks']],
                             [f'BR-0{index}' for index in range(1, 7)])
            self.assertEqual(payload['context']['participant_type'], 'thanh_vien_gia_lap')
            self.assertEqual(payload['status'], 'completed')
            self.assertTrue(all(row['elapsed_seconds'] >= 0 for row in payload['tasks']))
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)
            original = output.read_bytes()
            duplicate = subprocess.run([sys.executable, str(script), '--participant', 'test-P01',
                                        '--output', str(output)], input='', text=True,
                                       capture_output=True)
            self.assertNotEqual(duplicate.returncode, 0)
            self.assertEqual(output.read_bytes(), original)


    def test_interrupted_session_keeps_completed_task(self):
        script = Path(__file__).resolve().parents[2] / 'scripts' / 'record_usability.py'
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'interrupted.json'
            answers = ['thanh_vien_gia_lap', 'fixture role', 'test-version', 'fixture context',
                       '', '', 'khong_tro_giup', 'fixture answer', 'dung',
                       'fixture explanation', 'dung', 'fixture feedback']
            result = subprocess.run([sys.executable, str(script), '--participant', 'test-P02',
                                     '--output', str(output)], input='\n'.join(answers) + '\n',
                                    text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(output.read_text())
            self.assertEqual(payload['status'], 'in_progress')
            self.assertEqual(len(payload['tasks']), 1)
            self.assertEqual(payload['tasks'][0]['br'], 'BR-01')
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
