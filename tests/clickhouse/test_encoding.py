import unittest
from decimal import Decimal
from greenhouse.clickhouse.encoding import canonical_json, decimal_projection, digest, source_clock_us


class EncodingTest(unittest.TestCase):
    def test_decimal_preserves_precision_beyond_projection(self):
        value = '1.' + '1234567890' * 5
        exact, projected, status = decimal_projection(value)
        self.assertEqual(Decimal(exact), Decimal(value))
        self.assertIsNone(projected)
        self.assertEqual(status, 'outside_decimal256_projection')
        self.assertEqual(decimal_projection('9.3600'), ('9.36','9.36','exact'))
        self.assertEqual(decimal_projection(None), (None,None,'null'))
        self.assertEqual(decimal_projection('-0.000'), ('0','0','exact'))

    def test_rejects_binary_and_nonfinite_numbers(self):
        for value in (float('nan'), 9.36, 'NaN', 'Infinity'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decimal_projection(value)
        with self.assertRaises(ValueError):
            canonical_json({'nested':[0.1]})

    def test_hash_ignores_key_order_but_keeps_null_distinct_from_zero(self):
        self.assertEqual(digest({'a':1,'b':2}),digest({'b':2,'a':1}))
        self.assertNotEqual(digest({'v':None}),digest({'v':'0'}))

    def test_source_clock_has_no_host_timezone_conversion(self):
        self.assertEqual(source_clock_us('1970-01-01T00:00:00.000001'),1)
        self.assertEqual(source_clock_us('1969-12-31T23:59:59'),-1000000)
        with self.assertRaises(ValueError):
            source_clock_us('2020-03-01T00:00:00+00:00')
