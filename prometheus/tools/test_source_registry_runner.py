#!/usr/bin/env python3

import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import source_registry_runner as runner


class RegistryRunnerSandboxTest(unittest.TestCase):
    def test_sandbox_is_not_nested_under_private_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "sample"
            generator = profile / runner.GENERATOR_DIRECTORY
            generator.mkdir(parents=True)
            (profile / runner.MANIFEST_NAME).write_text(
                "\n".join(
                    [
                        "version: v1",
                        "profile: sample",
                        f"runner: {runner.RUNNER_ID}",
                        "upstreams:",
                        "  source:",
                        "    repository: owner/repository",
                        f"    commit: {'0' * 40}",
                        "    paths:",
                        "      - source.py",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            committed = b"version: v1\n"
            (profile / runner.REGISTRY_NAME).write_bytes(committed)
            (generator / runner.GENERATOR_ENTRYPOINT).write_text("print('version: v1')\n", encoding="utf-8")
            (generator / "test_negative_parser.py").write_text(
                "import unittest\n\nclass NegativeTest(unittest.TestCase):\n    pass\n",
                encoding="utf-8",
            )

            private_roots = []
            sandbox_roots = []

            def materialize(private_root, _upstream_root, _upstream_id, _upstream):
                self.assertEqual(stat.S_IMODE(private_root.stat().st_mode), 0o700)
                private_roots.append(private_root.resolve())

            def run_isolated(work, _command, capture):
                sandbox_roots.append(work.resolve())
                self.assertEqual(stat.S_IMODE(work.stat().st_mode), 0o555)
                return committed if capture else b""

            with (
                mock.patch.object(runner, "_materialize_upstream", side_effect=materialize),
                mock.patch.object(runner, "_run_isolated", side_effect=run_isolated),
            ):
                runner.verify_profile(profile)

        self.assertEqual(len(private_roots), 1)
        self.assertEqual(len(sandbox_roots), 2)
        self.assertNotIn(private_roots[0], sandbox_roots[0].parents)
        self.assertEqual(sandbox_roots[0].parent, Path("/tmp").resolve())


if __name__ == "__main__":
    unittest.main()
