import io
from pathlib import Path
import tempfile
import unittest
from operations.capture_notify import notify


class NotificationTest(unittest.TestCase):
    def test_credentials_in_body_only_and_no_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'credentials'
            p.write_text("export PUSHOVER_USER_KEY='fake-user'\nexport PUSHOVER_TOKEN=fake-token\n")
            calls=[]
            def accepted(request,timeout):
                calls.append(request)
                self.assertEqual(timeout,15)
                self.assertNotIn('fake-token',request.full_url)
                self.assertIn(b'token=fake-token',request.data)
                return io.BytesIO(b'{"status":1}')
            notify(p,open_url=accepted)
            self.assertEqual(len(calls),1)
            calls.clear()
            def failed(request,timeout):
                calls.append(request)
                raise TimeoutError('ambiguous transport')
            with self.assertRaises(TimeoutError):notify(p,open_url=failed)
            self.assertEqual(len(calls),1)
