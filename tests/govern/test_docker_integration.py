"""Opt-in REAL Docker tests; fixtures are synthetic, never the user's business repository."""
import os
from pathlib import Path
import tempfile
import unittest
from evoagent.govern.verification import DockerBackend, ExecutionProfile, VerificationGateway
from evoagent.runtime.artifacts import ArtifactStore
from evoagent.storage.store import TaskStore


@unittest.skipUnless(os.environ.get('GOVERN_TEST_IMAGE'), 'set GOVERN_TEST_IMAGE to a prepared digest-pinned Pytest image')
class DockerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.store=TaskStore(str(Path(self.directory.name)/'db.sqlite'));self.store.create('test','fixture',None,{})
        self.artifacts=ArtifactStore(self.store)
        self.gateway=VerificationGateway(self.artifacts,{'python-pytest':ExecutionProfile(os.environ['GOVERN_TEST_IMAGE'])})
        self.assertTrue(self.gateway.backend.available(),'Docker Linux engine must be available for opted-in tests')
        common={'gateway.py':'def rate():\n    raise RuntimeError("external service")\n'}
        self.snapshot={'snapshot_id':'synthetic-fixture','base_files':{**common,'pricing.py':'import gateway\ndef total(qty):\n    return qty * gateway.rate()\n'},
            'head_files':{**common,'pricing.py':'import gateway\ndef total(qty):\n    return qty + gateway.rate()\n'}}
        self.ref=self.artifacts.put('test','default','code-snapshot',self.snapshot).uri

    def run_bundle(self,source,compare='head'):
        plan=self.artifacts.put('test','default','verification-plan',{'hypothesis':'Quantity times external rate',
            'assertion_source':'Synthetic fixture contract, not a production PRD','target_symbols':['pricing.total'],
            'mocked_symbols':['gateway.rate'],'selectors':['.govern-tests/test_check.py']}).uri
        bundle=self.artifacts.put('test','default','verification-tests',{'test_check.py':source}).uri
        return self.gateway.run('test','default','validator',self.ref,{'finding_id':'fixture-finding','snapshot_id':'synthetic-fixture',
            'test_plan_ref':plan,'test_bundle_ref':bundle,'execution_profile':'python-pytest','compare':compare})

    def test_same_external_mock_bundle_passes_base_and_fails_head(self):
        result=self.run_bundle('from unittest.mock import patch\nfrom pricing import total\ndef test_total():\n    with patch("gateway.rate", return_value=2):\n        assert total(3) == 6\n','base_and_head')
        self.assertEqual('completed',result['execution_status'],result)
        self.assertEqual('passed',result['base']['test_status'],result)
        self.assertEqual('failed',result['head']['test_status'],result)
        self.assertEqual('removed',result['head']['cleanup'])

    def test_zero_tests_not_reported_as_pass(self):
        result=self.run_bundle('# no executable tests\n')
        self.assertEqual('not_collected',result['head']['test_status'],result)

    def test_import_failure_not_reported_as_reproduction(self):
        result=self.run_bundle('import govern_nonexistent_dependency_for_test\ndef test_x(): pass\n')
        self.assertEqual('not_run',result['head']['test_status'],result)

    def test_nonroot_readonly_input_no_socket_and_network_disabled(self):
        source='''import os, socket
from pathlib import Path
def test_isolation():
    assert os.geteuid() != 0
    assert not Path('/var/run/docker.sock').exists()
    try:
        Path('/input/pricing.py').write_text('overwritten')
    except OSError:
        pass
    else:
        raise AssertionError('input was writable')
    try:
        connection=socket.create_connection(('1.1.1.1',443),timeout=.2)
    except OSError:
        pass
    else:
        connection.close()
        raise AssertionError('external network was reachable')
'''
        result=self.run_bundle(source)
        self.assertEqual('passed',result['head']['test_status'],result)


if __name__=='__main__':unittest.main()
