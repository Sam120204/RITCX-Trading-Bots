import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ritcx import runtime
from ritcx.runtime import TradingError


class RuntimeTests(unittest.TestCase):
    def test_practice_profile_rejects_changed_endpoint(self):
        cfg = dict(
            RIT_API_URL="http://example.com:16520/v1", RIT_USERNAME="test", RIT_PASSWORD="secret"
        )
        with self.assertRaises(TradingError):
            runtime.validate_profile(cfg, practice=True)

    def test_formal_profile_requires_explicit_matching_mode(self):
        cfg = dict(
            RIT_ENVIRONMENT="contest",
            RIT_API_URL="http://example.com:17000/v1",
            RIT_USERNAME="test",
            RIT_PASSWORD="secret",
        )
        with self.assertRaises(TradingError):
            runtime.validate_profile(cfg, practice=True)
        runtime.validate_profile(cfg, contest=True)
        cfg["RIT_API_URL"] = "http://example.com:17000/v1?password=secret"
        with self.assertRaises(TradingError):
            runtime.validate_profile(cfg, contest=True)

    def test_read_only_still_rejects_unconfigured_credentials(self):
        with self.assertRaises(TradingError):
            runtime.validate_profile({"RIT_API_URL": runtime.PRACTICE_URL})

    def test_client_rest_requires_api_key_instead_of_password(self):
        cfg = dict(
            RIT_ENVIRONMENT="contest",
            RIT_API_KIND="rest",
            RIT_API_URL="http://localhost:9999/v1",
            RIT_USERNAME="team-account",
            RIT_API_KEY="local-key",
        )
        runtime.validate_profile(cfg, contest=True)
        cfg.pop("RIT_API_KEY")
        with self.assertRaises(TradingError):
            runtime.validate_profile(cfg, contest=True)

    def test_credentials_can_be_supplied_by_environment_without_a_file(self):
        from unittest.mock import patch

        with patch.dict(os.environ, {"RIT_USERNAME": "env-user", "RIT_PASSWORD": "env-secret"}):
            cfg = runtime.read_config(Path("/does/not/exist.env"))
        self.assertEqual(cfg["RIT_USERNAME"], "env-user")
        self.assertEqual(cfg["RIT_PASSWORD"], "env-secret")

    def test_account_lock_blocks_another_process_and_releases_on_exit(self):
        cfg = {"RIT_USERNAME": "test"}
        with tempfile.TemporaryDirectory() as directory:
            child_code = """
from pathlib import Path
from ritcx.runtime import account_lock
with account_lock({'RIT_USERNAME':'test'},directory=Path(__import__('sys').argv[1])):
    print('acquired')
"""
            with runtime.account_lock(cfg, directory=Path(directory)):
                result = subprocess.run(
                    [sys.executable, "-c", child_code, directory], capture_output=True, text=True
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("acquired", result.stdout)
            result = subprocess.run(
                [sys.executable, "-c", child_code, directory], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("acquired", result.stdout)

    def test_runner_can_share_lock_with_child_but_not_unrelated_descriptor(self):
        cfg = {"RIT_USERNAME": "test"}
        with tempfile.TemporaryDirectory() as directory:
            with runtime.account_lock(cfg, directory=Path(directory)) as fd:
                with runtime.account_lock(
                    cfg, inherited_fd=fd, directory=Path(directory)
                ) as inherited:
                    self.assertEqual(os.fstat(fd).st_ino, os.fstat(inherited).st_ino)
                # Closing the inherited copy must leave the parent's lock held.
                with self.assertRaises(TradingError):
                    with runtime.account_lock(cfg, directory=Path(directory)):
                        pass
            with open(Path(directory) / "other", "w") as unrelated:
                with self.assertRaises(TradingError):
                    with runtime.account_lock(
                        cfg, inherited_fd=unrelated.fileno(), directory=Path(directory)
                    ):
                        pass


if __name__ == "__main__":
    unittest.main()
