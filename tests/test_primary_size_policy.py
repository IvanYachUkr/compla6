"""Standard codec availability must never waive custom reconstruction bytes."""
import unittest
from compression_lab import deployment
from compression_lab.workloads.bridge import rank_cost


class PrimarySizeTests(unittest.TestCase):
    def test_only_pinned_decoder_libraries_are_deducted(self):
        self.assertTrue(hasattr(deployment, 'decoder_views'))
        fixed = {'decoder': {'nonplatform_decoder_dependency_bytes': 500,
            'dependency_inventory': {'libraries': [
                {'soname':'libzstd.so.1','sha256':'a'*64,'bytes':300,'platform':False},
                {'soname':'libcustom.so.1','sha256':'b'*64,'bytes':200,'platform':False}]}}}
        strict = {'corpus': {'actual': {'deployment_total_bytes': 2000,
            'compiled_decoder_bytes':400, 'required_decoder_artifact_bytes':100, 'archive_bytes':1000}}}
        scenario = {'schema_version':1,'profiles':[{'id':'standard-codec-available-v1',
            'libraries':[{'soname':'libzstd.so.1','sha256':'a'*64}]}]}
        view = deployment.decoder_views(fixed, strict, scenario)
        self.assertEqual(view['corpus']['actual']['deployment_total_bytes'], 1700)
        self.assertEqual(view['corpus']['actual']['compiled_decoder_bytes'], 400)
        self.assertEqual(view['corpus']['actual']['required_decoder_artifact_bytes'], 100)
        self.assertEqual(view['corpus']['strict_deployment_total_bytes'], 2000)
        scenario['profiles'][0]['libraries'][0]['sha256'] = 'c'*64
        self.assertEqual(deployment.decoder_views(fixed, strict, scenario)['corpus']['actual']['deployment_total_bytes'], 2000)

    def test_declared_primary_view_drives_rank_without_reinterpreting_old_results(self):
        raw = {'evaluation_mode':'whole_dataset','accounting_policy':'supervisor-decoder-v1',
               'decoder_accounting': {'corpus': {'actual': {'deployment_total_bytes':2000}}},
               'reported_accounting': {'corpus': {'actual': {'deployment_total_bytes':1700}}}}
        self.assertEqual(rank_cost(raw), 2000)
        raw['primary_size_policy'] = 'standard-codec-available-v1'
        self.assertEqual(rank_cost(raw), 1700)

    def test_paired_feedback_reconciles_primary_and_strict_totals(self):
        from compression_lab import accounting
        from test_paired_diagnostics import result
        reference, trial = result([10,100,30]), result([5,90,25])
        for raw, library_bytes in ((reference,300),(trial,0)):
            raw.update(primary_size_policy='standard-codec-available-v1', accounting_policy='supervisor-decoder-v1')
            fixed = raw['fixed_costs']
            fixed['installed_runtime_policy'] = 'test'
            fixed['decoder'] = {'compiled_decoder_bytes':100,'required_decoder_artifact_bytes':10,
                'nonplatform_decoder_dependency_bytes':library_bytes,
                'dependency_inventory':{'libraries':[{'soname':'libzstd.so.1','sha256':'a'*64,'bytes':library_bytes,'platform':False}]}}
            n = sum(r['canonical_bytes'] for r in raw['objects'])
            a = raw['accounting']['development']['actual']['archive_bytes']
            raw['decoder_accounting'] = {'development':accounting.decoder_costs(n,a,fixed,1000000)}
            raw['reported_accounting'] = deployment.decoder_views(fixed,raw['decoder_accounting'],
                {'schema_version':1,'profiles':[{'id':'standard-codec-available-v1','libraries':[{'soname':'libzstd.so.1','sha256':'a'*64}]}]})
        report = accounting.paired_diagnostics(reference,trial)
        self.assertEqual(report['strict_deployment_delta_bytes'], -315)
        self.assertEqual(report['deployment_delta_bytes'], -15)
        self.assertEqual(sum(report['deployment_components_delta_bytes'].values()), -15)


if __name__ == '__main__': unittest.main()
