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
        self.assertIn('the shell button beside **Connect**', highlights)
        self.assertIn('`qsub`', highlights)
        self.assertIn('submissions', highlights)
        self.assertIn('are refused', highlights)
        self.assertIn(
            'Desktops launched before the upgrade get Terminal the next time they start',
            highlights,
        )
        self.assertIn('**Shell**', highlights)
        self.assertNotIn('Terminal page', highlights)
        self.assertIn('not retried immediately', operators)
        self.assertNotIn('Updated web portal components', highlights)
        self.assertNotIn('IDEA-', highlights)
        self.assertIn('Select **Start** again', highlights)
        self.assertIn('If hibernation is disabled', highlights)
        self.assertIn('**Update session**', highlights)
        self.assertIn('same instance family', highlights)
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

    def test_maintenance_wording_matches_renderers(self):
        for path in (
            'docs/modules/cluster-manager/maintenance-banner.md',
            'source/idea/ideactl/resources/config/templates/cluster-manager/settings.yml',
        ):
            text = (ROOT / path).read_text()
            self.assertNotIn('"until', text)
            self.assertIn('Expected to end <', text)
            self.assertIn('YYYY-MM-DD HH:MM', text)
            self.assertIn('UTC', text)

    def test_completion_timing_guidance_uses_one_timestamp_source(self):
        text = (ROOT / 'docs/changelog.md').read_text()
        self.assertNotIn('recover missing timestamps from cached', text)
        self.assertIn('latest execution attempt', text)
        self.assertIn('neither timestamp', text)
        self.assertIn('missing from fresh PBS history is never filled', text)
        self.assertIn('job-completion-timing.md', text)

    def test_maintenance_exemptions_include_validation_user(self):
        for path in (
            'docs/changelog.md',
            'docs/first-time-users/cluster-operations/update-idea-cluster/upgrade-cluster.md',
        ):
            with self.subTest(path=path):
                text = (ROOT / path).read_text()
                self.assertIn('configured image-pipeline validation user', text)
                self.assertIn('default `idea-validate`', text)
                self.assertIn('maintenance-banner.md', text)

    def test_job_metric_guidance_distinguishes_agent_and_http_tags(self):
        for path in (
            'docs/changelog.md',
            'docs/first-time-users/cluster-operations/update-idea-cluster/ecs-control-plane.md',
        ):
            with self.subTest(path=path):
                text = (ROOT / path).read_text()
                self.assertNotIn('without `env` for live sends and backfills', text)
                self.assertNotIn(
                    'for both live sends and backfills at every timestamp', text
                )
                self.assertIn('origin detection', text)
                self.assertIn('service:scheduler', text)
                self.assertIn('HTTP backfills', text)
                self.assertIn('double-count', text)

    def test_operator_upgrade_remedies(self):
        text = (ROOT / 'docs/changelog.md').read_text()
        self.assertNotIn('Existing desktops require updated bootstrap trust', text)
        self.assertIn('get the same trust from the desktop refresh', text)
        self.assertNotIn('cannot be refreshed in place', text)
        self.assertIn('`--accept-config-drift` for unattended runs', text)
        self.assertIn('update `values.yml` first', text)

    def test_command_reference_explains_mandatory_govcloud_digest(self):
        text = (ROOT / 'source/idea/ideactl/docs/COMMANDS.md').read_text()
        for section in text.split('`--skip-release-image-check` skips ')[1:]:
            paragraph = section.split('\n\n', 1)[0]
            self.assertIn('except on GovCloud', paragraph)
            self.assertIn('tag@sha256:', paragraph)
        self.assertEqual(text.count('except on GovCloud'), 2)

    def test_fresh_govcloud_tag_limit_is_explicit(self):
        for path in (
            'docs/changelog.md',
            'source/idea/ideactl/resources/config/templates/ecs/settings.yml',
        ):
            text = (ROOT / path).read_text()
            self.assertIn('Fresh GovCloud installs', text)
            self.assertIn('mutable public tag until', text)
            self.assertIn('first upgrade', text)

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
