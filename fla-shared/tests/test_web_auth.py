import base64
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import web_auth


class TestEnsureCredentials(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.auth_file = os.path.join(self.tmpdir, "webui-auth.json")

    def tearDown(self):
        if os.path.exists(self.auth_file):
            os.unlink(self.auth_file)
        os.rmdir(self.tmpdir)

    def test_creates_credentials_file_when_missing(self):
        creds = web_auth.ensure_credentials(self.auth_file)
        self.assertTrue(os.path.exists(self.auth_file))
        self.assertEqual(creds["username"], web_auth.DEFAULT_USERNAME)
        self.assertTrue(creds["password"])

    def test_reuses_existing_credentials(self):
        first = web_auth.ensure_credentials(self.auth_file)
        second = web_auth.ensure_credentials(self.auth_file)
        self.assertEqual(first, second)


class TestIsAuthorized(unittest.TestCase):
    def _headers(self, username, password):
        token = base64.b64encode(("%s:%s" % (username, password)).encode()).decode()
        return {"Authorization": "Basic " + token}

    def test_accepts_matching_basic_auth_header(self):
        creds = {"username": "victron", "password": "secret"}
        self.assertTrue(web_auth.is_authorized(self._headers("victron", "secret"), creds))

    def test_rejects_missing_header(self):
        creds = {"username": "victron", "password": "secret"}
        self.assertFalse(web_auth.is_authorized({}, creds))

    def test_rejects_wrong_password(self):
        creds = {"username": "victron", "password": "secret"}
        self.assertFalse(web_auth.is_authorized(self._headers("victron", "wrong"), creds))

    def test_rejects_malformed_header(self):
        creds = {"username": "victron", "password": "secret"}
        self.assertFalse(web_auth.is_authorized({"Authorization": "Basic not-base64"}, creds))


if __name__ == '__main__':
    unittest.main()
