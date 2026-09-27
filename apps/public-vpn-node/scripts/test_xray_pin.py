"""Offline pin mutation tests; no Docker or real config reads."""
import copy
import unittest
from check_xray_pin import DIGEST, REPO, TAG, check, validate


class PinTests(unittest.TestCase):
    def setUp(self):
        self.lock = dict(version='26.3.27', tag=TAG, reality_revision='9234c772ba8f',
                         repo_digest=DIGEST, status='verified-root-pull', platform='linux/amd64')
        self.compose = {'services': {'ingress': {'image': DIGEST}}}

    def test_public_files(self):
        self.assertEqual(check(), [])

    def test_exact_pin(self):
        self.assertEqual(validate(self.lock, self.compose), [])

    def test_unresolved_is_never_deployable(self):
        self.lock.update(repo_digest=None, status='pending-root-pull')
        self.compose['services']['ingress']['image'] = TAG + '@sha256:REPLACE_WITH_VERIFIED_26_3_27_REPODIGEST'
        self.assertTrue(validate(self.lock, self.compose))

    def test_image_mutations(self):
        for image in (TAG, REPO + ':latest', '${XRAY_IMAGE}',
                      TAG + '@sha256:' + DIGEST.rsplit(':', 1)[1],
                      REPO + ':26.9.8', REPO + ':26.7.28', None,
                      REPO + '@sha256:' + 'abcdef0123456789' * 4):
            with self.subTest(image=image):
                self.compose['services']['ingress']['image'] = image
                self.assertTrue(validate(self.lock, self.compose))

    def test_lock_mutations(self):
        for key, value in [('version', '26.9.8'), ('tag', REPO + ':26.7.28'),
                           ('reality_revision', '8cdf7bf9c7f0'), ('status', 'pending-root-pull'),
                           ('platform', 'linux/arm64'), ('platform', None),
                           ('repo_digest', TAG), ('repo_digest', REPO + '@sha256:bad')]:
            with self.subTest(key=key, value=value):
                changed = copy.deepcopy(self.lock)
                changed[key] = value
                self.assertTrue(validate(changed, self.compose))

    def test_matching_unapproved_digest_rejected(self):
        for sha in ('3629bf7d825748cda29698ac354f8f2146f6b292edb9eb0c7cb7fe0583dae091',
                    '0123456789abcdef' * 4, '0' * 64):
            with self.subTest(sha=sha):
                self.lock['repo_digest'] = REPO + '@sha256:' + sha
                self.compose['services']['ingress']['image'] = self.lock['repo_digest']
                self.assertTrue(validate(self.lock, self.compose))

    def test_missing_required_fields(self):
        for key in self.lock:
            changed = copy.deepcopy(self.lock)
            del changed[key]
            self.assertTrue(validate(changed, self.compose))
        self.assertTrue(validate(self.lock, {'services': {}}))


if __name__ == '__main__':
    unittest.main()
