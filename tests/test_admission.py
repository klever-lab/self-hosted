"""Deployment must fail closed before applying unguarded qBittorrent workloads."""

import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import manage


class AdmissionTests(unittest.TestCase):
    def objects(self):
        return [
            {'kind': 'Namespace', 'metadata': {'name': 'qbittorrent'}},
            {'kind': 'ValidatingAdmissionPolicy', 'metadata': {'name': 'self-hosted-qbittorrent-security'}},
            {'kind': 'ValidatingAdmissionPolicyBinding', 'spec': {
                'policyName': 'self-hosted-qbittorrent-security', 'validationActions': ['Deny']}},
        ]

    def test_missing_guard_cannot_relax_namespace(self):
        items = self.objects()
        for missing in (1, 2):
            with self.subTest(missing=missing), patch.object(manage, 'apply') as apply:
                with self.assertRaisesRegex(ValueError, 'enforcing admission'):
                    manage.install_admission([obj for i, obj in enumerate(items) if i != missing])
                apply.assert_not_called()

    def test_type_errors_prevent_binding(self):
        current = {'metadata': {'generation': 2}, 'status': {
            'observedGeneration': 2, 'typeChecking': {'expressionWarnings': [{'warning': 'bad expression'}]}}}
        with patch.object(manage, 'apply') as apply, patch.object(manage, 'kube', return_value=
                subprocess.CompletedProcess([], 0, json.dumps(current).encode(), b'')):
            with self.assertRaisesRegex(ValueError, 'type errors'):
                manage.install_admission(self.objects())
        self.assertEqual(apply.call_count, 1)
        self.assertEqual(apply.call_args.args[0][0]['kind'], 'ValidatingAdmissionPolicy')

    def test_stale_type_check_cannot_enable_binding(self):
        current = {'metadata': {'generation': 2}, 'status': {'observedGeneration': 1, 'typeChecking': {}}}
        with patch.object(manage, 'apply') as apply, patch.object(manage, 'time') as clock, \
                patch.object(manage, 'kube', return_value=subprocess.CompletedProcess([], 0, json.dumps(current).encode(), b'')):
            clock.monotonic.side_effect = [0, 31]
            with self.assertRaisesRegex(RuntimeError, 'finish type checking'):
                manage.install_admission(self.objects())
        self.assertEqual(apply.call_count, 1)

    def test_warning_only_binding_cannot_relax_namespace(self):
        items = self.objects()
        items[-1]['spec']['validationActions'] = ['Warn']
        with patch.object(manage, 'apply') as apply, self.assertRaisesRegex(ValueError, 'enforcing admission'):
            manage.install_admission(items)
        apply.assert_not_called()

    def deployment(self):
        return {'kind': 'Deployment', 'metadata': {'namespace': 'qbittorrent'},
                'spec': {'template': {'metadata': {'labels': {'app': 'qbittorrent'}},
                                      'spec': {'containers': [{'name': 'qbittorrent', 'image': 'test-image'}]}}}}

    def test_positive_check_requires_an_actual_denial_first(self):
        items = self.objects() + [self.deployment()]
        error = b"ValidatingAdmissionPolicy 'self-hosted-qbittorrent-security' denied request: forbidden"
        with patch.object(manage, 'kube', side_effect=[
                subprocess.CompletedProcess([], 0, b'{}', b''),
                subprocess.CompletedProcess([], 1, b'', error),
                subprocess.CompletedProcess([], 0, b'', b'')]) as kube:
            manage.check_admission_guard(items)
            manage.check_admission(items)
        probe = json.loads(kube.call_args_list[1].kwargs['data'])
        valid = json.loads(kube.call_args_list[2].kwargs['data'])
        self.assertFalse(probe['spec']['containers'][0]['securityContext']['readOnlyRootFilesystem'])
        self.assertNotIn('securityContext', valid['spec']['containers'][0])
        self.assertNotIn('hostNetwork', items[-1]['spec']['template']['spec'])

    def test_unrelated_admission_error_does_not_count_as_security(self):
        with patch.object(manage, 'kube', side_effect=[
                subprocess.CompletedProcess([], 0, b'{}', b''),
                subprocess.CompletedProcess([], 1, b'', b'namespace not found')]):
            with self.assertRaisesRegex(RuntimeError, 'unexpectedly'):
                manage.check_admission_guard(self.objects() + [self.deployment()])

    def test_fail_open_binding_stops_deployment_check(self):
        with patch.object(manage, 'kube', return_value=subprocess.CompletedProcess([], 0, b'{}', b'')), \
                patch.object(manage, 'time') as clock:
            clock.monotonic.side_effect = [0, 31]
            with self.assertRaisesRegex(RuntimeError, 'did not reject'):
                manage.check_admission_guard(self.objects() + [self.deployment()])

    def test_new_namespace_stays_restricted_until_guard_is_proven(self):
        error = b"ValidatingAdmissionPolicy 'self-hosted-qbittorrent-security' denied request: forbidden"
        with patch.object(manage, 'apply') as apply, patch.object(manage, 'kube', side_effect=[
                subprocess.CompletedProcess([], 0, b'', b''),
                subprocess.CompletedProcess([], 0, b'', b''),
                subprocess.CompletedProcess([], 1, b'', error)]):
            manage.check_admission_guard(self.objects() + [self.deployment()])
        namespace, = apply.call_args.args[0]
        self.assertEqual(namespace['metadata']['labels']['pod-security.kubernetes.io/enforce'], 'restricted')

    def test_pods_from_cronjobs_keep_their_namespace_and_security(self):
        template = self.deployment()['spec']['template']
        cron = {'kind': 'CronJob', 'metadata': {'namespace': 'qbittorrent'},
                'spec': {'jobTemplate': {'spec': {'template': template}}}}
        pod, = manage.pod_templates([cron])
        self.assertEqual(pod['metadata']['namespace'], 'qbittorrent')
        self.assertEqual(pod['spec'], template['spec'])

    def test_pod_check_is_strict_and_cannot_create_a_workload(self):
        pod, = manage.pod_templates([self.deployment()])
        with patch.object(manage, 'kube') as kube:
            manage.dry_run_pod(pod)
        self.assertIn('/api/v1/namespaces/qbittorrent/pods?dryRun=All&fieldValidation=Strict', kube.call_args.args)
        self.assertEqual(json.loads(kube.call_args.kwargs['data']), pod)


if __name__ == '__main__':
    unittest.main()
