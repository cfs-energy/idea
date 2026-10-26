"""Keep the release's user guidance separate from deployment details."""

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ReleaseDocsTests(unittest.TestCase):
    def test_26111_user_highlights_and_upgrade_floor(self):
        release = (
            (ROOT / 'docs/changelog.md')
            .read_text()
            .split('## [26.11.1]', 1)[1]
            .split('## [26.10.9]', 1)[0]
        )
        banner, highlights = release.split('**Highlights for users**', 1)
        self.assertIn('**Upgrade floor: 26.10.0.**', banner)
        highlights, operators = highlights.strip().split('\n\n', 1)
        self.assertIn('**Terminal**', highlights)
        self.assertIn('`qsub`', highlights)
        self.assertIn('submissions', highlights)
        self.assertIn('are refused', highlights)
        self.assertIn('Desktops launched before the upgrade are not listed', highlights)
        self.assertIn('not retried immediately', operators)
        self.assertNotIn('Updated web portal components', highlights)
        self.assertNotIn('IDEA-', highlights)
        self.assertIn('retry', highlights)
        for ticket in (263, 273, 285, 297, 298, 299):
            self.assertIn(f'IDEA-{ticket}:', operators)
        self.assertNotIn('Open **Terminal**', operators)
        self.assertNotIn('Direct `qsub` submissions', operators)

    def test_upgrade_maintenance_message_matches_both_submission_paths(self):
        guide = (
            ROOT
            / 'docs/first-time-users/cluster-operations/update-idea-cluster/upgrade-cluster.md'
        ).read_text()
        self.assertIn(
            'Portal and `qsub` submissions from non-admin users are refused', guide
        )
        self.assertIn('Cluster is in maintenance: <message>', guide)
        self.assertIn('ends_at', guide)
        self.assertNotIn('try again later', guide)

    def test_govcloud_repository_guidance_matches_selection_and_verification(self):
        for name in ('upgrade-cluster.md', 'ecs-control-plane.md'):
            guide = (
                ROOT
                / 'docs/first-time-users/cluster-operations/update-idea-cluster'
                / name
            ).read_text()
            self.assertNotIn('repository authoritative', guide)
            self.assertIn('fresh installs default to the public release image', guide)
            self.assertIn('--skip-release-image-check', guide)
            self.assertIn('ecs.release_image', guide)
            self.assertIn('in-boundary registry', guide)
        template = (
            ROOT / 'source/idea/ideactl/resources/config/templates/ecs/settings.yml'
        ).read_text()
        self.assertNotIn("Deploy uses this partition's repository", template)
        self.assertIn('Upgrade-only repository selection', template)

    def test_install_has_one_upgrade_banner(self):
        install = (ROOT / 'docs/first-time-users/install-idea/README.md').read_text()
        self.assertEqual(install.count('The control plane runs only on ECS.'), 1)

    def test_feature_guides_omit_unrelated_upgrade_banner(self):
        for path in (
            'modules/cluster-manager/users-management.md',
            'modules/cluster-manager/menu/users-management.md',
            'modules/hpc-workloads/admin-documentation/custom-amis.md',
            'modules/hpc-workloads/admin-documentation/configure-floating-license-resources.md',
        ):
            with self.subTest(path=path):
                self.assertNotIn(
                    'The control plane runs only on ECS.',
                    (ROOT / 'docs' / path).read_text(),
                )

    def test_bug_report_requests_supported_service_logs(self):
        guide = (ROOT / 'docs/help-and-support/raise-a-bug.md').read_text()
        self.assertIn('container service log stream', guide)
        self.assertNotIn('/opt/idea/app/logs/application.log', guide)

    def test_backup_retention_grammar(self):
        guide = (
            ROOT / 'docs/best-practices/security/backup-idea-environment.md'
        ).read_text()
        self.assertIn('keeps the backups for 7 days', guide)


if __name__ == '__main__':
    unittest.main()
