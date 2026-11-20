"""Check release guidance against the shipped ECS-only commands and tasks."""

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DocsCiTests(unittest.TestCase):
    def test_upgrade_guidance_matches_recovery_guard(self):
        guide = (ROOT / 'source/idea/ideactl/docs/COMMANDS.md').read_text()
        for obsolete in (
            'The cutover gate',
            'The scheduler cutover gate',
            'upgrade saves its original',
            'ECS module-set rows remain held',
            'With `enable_ecs: true`, the bastion moves',
        ):
            self.assertNotIn(obsolete, guide)
        self.assertIn('`cluster.upgrade_historical_pending`', guide)
        self.assertIn('26.10.9 administrator', guide)

    def test_onboarding_lists_every_suite(self):
        guide = (ROOT / 'docs/developer-portal/developer-onboarding.md').read_text()
        self.assertIn('`mise run test:data-model`', guide)
        self.assertIn('all six suites', guide)

    def test_pre_floor_migration_is_removed(self):
        self.assertFalse((ROOT / 'scripts/pre-upgrade-317.sh').exists())

    def test_install_commands_name_files_that_exist(self):
        guide = (
            ROOT / 'docs/first-time-users/install-idea/standard-installation.md'
        ).read_text()
        blocks = re.findall(r'```[a-z]*\n(.*?)```', guide, re.S)
        named = set(re.findall(r'\./([\w.-]+\.(?:sh|ps1))\b', '\n'.join(blocks)))
        self.assertIn('idea-admin.sh', named)
        for name in named:
            self.assertTrue((ROOT / name).is_file(), name)
        for block in blocks:
            if 'git clone' in block:
                self.assertIn(
                    '--branch v<VERSION>',
                    block,
                    'a clone of the default branch is not the release',
                )


if __name__ == '__main__':
    unittest.main()
