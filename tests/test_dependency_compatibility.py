"""Exercise real SDKs; CI runs this against ERP's SDK and a newer SDK."""

import base64
import hashlib
import importlib.util
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from botocore.config import Config
from botocore.awsrequest import AWSResponse
from botocore.exceptions import FlexibleChecksumError
from botocore.stub import Stubber
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.exceptions import InvalidSignature

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
fake_frappe = SimpleNamespace(whitelist=lambda **kwargs: lambda function: function)
spec = importlib.util.spec_from_file_location(
    "frappe_controller.api._s3_dependency_test",
    ROOT / "frappe_controller/api/s3_routes.py",
)
s3 = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"frappe": fake_frappe}):
    spec.loader.exec_module(s3)


class DependencyCompatibilityTests(unittest.TestCase):
    def setUp(self):
        # Explicit dummy credentials ensure client construction never uses IMDS.
        self.settings = SimpleNamespace(
            aws_access_key_id="testing", aws_secret_access_key="testing",
            aws_region="us-east-1", s3_bucket="test-bucket",
            s3_allowed_buckets=("test-bucket",), s3_allowed_prefixes=("",),
            s3_max_object_bytes=1024, s3_presigned_url_seconds=60,
        )
        self.client = s3._client(self.settings)
        self.addCleanup(self.client.close)
        self.parameters = {
            "Bucket": "test-bucket", "Key": "source/backup.sql.gz", "VersionId": "v1",
        }
        self.expected = {**self.parameters, "ChecksumMode": "ENABLED"}

    def execute(self, action):
        with patch.object(s3, "_client", return_value=self.client):
            return s3._execute(action, self.parameters, {}, "source", self.settings)

    def test_real_sdk_client_construction_and_retry_policy(self):
        self.assertEqual("standard", self.client.meta.config.retries["mode"])
        if "response_checksum_validation" in Config.OPTION_DEFAULTS:
            self.assertEqual("when_supported", self.client.meta.config.response_checksum_validation)

    def test_versioned_head_requests_checksums(self):
        with Stubber(self.client) as stubber:
            stubber.add_response("head_object", {
                "ContentLength": 8, "VersionId": "v1", "ChecksumSHA256": "checksum",
            }, self.expected)
            self.assertEqual("checksum", self.execute("head_object")["ChecksumSHA256"])
            stubber.assert_no_pending_responses()

    def test_presign_preserves_checksum_and_version_binding(self):
        with Stubber(self.client) as stubber:
            stubber.add_response("head_object", {
                "ContentLength": 8, "VersionId": "v1", "ChecksumSHA256": "checksum",
            }, self.expected)
            response = self.execute("presign_get_object")
            self.assertEqual("x-amz-checksum-sha256", response["ChecksumHeader"])
            self.assertEqual("checksum", response["ChecksumValue"])
            self.assertIn("versionId=v1", response["URL"])
            stubber.assert_no_pending_responses()

    def test_missing_or_unrecognized_checksum_still_fails_closed(self):
        with Stubber(self.client) as stubber:
            stubber.add_response("head_object", {"ContentLength": 8}, self.expected)
            with self.assertRaises(s3.ControllerRequestError) as caught:
                self.execute("presign_get_object")
            self.assertEqual("s3_checksum_required", caught.exception.code)

    def test_unversioned_object_is_rejected(self):
        self.parameters.pop("VersionId")
        with self.assertRaises(s3.ControllerRequestError) as caught:
            self.execute("head_object")
        self.assertEqual("s3_version_required", caught.exception.code)

    def test_real_sdk_rejects_corrupted_download_with_checksum_mode(self):
        checksum = base64.b64encode(hashlib.sha256(b"original").digest()).decode()
        for payload in (b"original", b"tampered"):
            with self.subTest(payload=payload):
                response = AWSResponse("https://test-bucket.s3.amazonaws.com/object", 200, {
                    "content-length": str(len(payload)), "x-amz-checksum-sha256": checksum,
                }, io.BytesIO(payload))
                with patch.object(self.client._endpoint.http_session, "send", return_value=response):
                    body = self.client.get_object(**self.expected)["Body"]
                    try:
                        if payload == b"original":
                            self.assertEqual(payload, body.read())
                        else:
                            with self.assertRaises(FlexibleChecksumError):
                                body.read()
                    finally:
                        body.close()

    def test_crypto_signatures_reject_tampering(self):
        private = Ed25519PrivateKey.generate()
        signature = private.sign(b"controller-request")
        private.public_key().verify(signature, b"controller-request")
        with self.assertRaises(InvalidSignature):
            private.public_key().verify(signature, b"tampered-request")


if __name__ == "__main__":
    unittest.main()
