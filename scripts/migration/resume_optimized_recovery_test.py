"""Attempt-6 continuation preserves cohort and exactly-once recovery semantics."""
import copy
import importlib.util
import pathlib
import types
import unittest
from unittest import mock


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name(file))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


W = load('optimized_wrapper', 'resume_optimized_recovery.py')
T = load('optimized_reused_faults', 'resume_attested_recovery_test.py')
R = W.load_derived(pathlib.Path(__file__).with_name('resume_idle_recovery.py').read_bytes())


class Host(T.Host):
    def __init__(self, failure=None):
        super().__init__(failure)
        record = {'v':1, 'run':R.RUN, 'owner':R.OWNER, 'id':'604dbd5e9caf0536f392529591ff876d',
                  'previous_state':self.initial, 'previous_state_sha256':T.T.E.digest(self.initial), 'proof_sha256':'c'*64}
        raw = T.T.A.canonical(record); self.records[record['id']] = raw
        self.initial = {**self.initial, 'attempt':6, 'pause_seconds':R.LIFETIME,
                        'budget_cohort':{'id':record['id'], 'record_sha256':R.sha(raw)}}
        self.raw = T.T.A.canonical(self.initial); self.frozen['state.json'] = self.raw
        self.target.update(ledger_epoch=24, restored_ingress_epoch=6)

    def observe(self, name):
        result = super().observe(name)
        (result['ingress'] if name == 'runtime.status' else result)['epoch'] = 6
        return result

    def start_main(self):
        super().start_main()
        if self.failure not in ('monitor_timeout', 'main_failed'):
            state = T.T.A.strict(self.raw); state.update(attempt=7, pause_seconds=R.LIFETIME+4)
            self.raw = T.T.A.canonical(state)


class OptimizedContinuationTests(unittest.TestCase):
    def test_exact_boundary_and_immutable_base(self):
        raw = pathlib.Path(__file__).with_name('resume_idle_recovery.py').read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'base-changed'): W.derive(raw+b'\n')
        host = Host(); R.source_state(T.T.A, host.raw, host.engine)
        for key, value in [('attempt',5), ('pause_seconds',23.854798037558794), ('pg','TENCENT')]:
            with self.assertRaises(RuntimeError): R.source_state(T.T.A,T.T.A.canonical({**host.initial,key:value}),host.engine)

    def test_old_cohort_history_is_preserved_and_start_is_once(self):
        host = Host(); before = copy.deepcopy(host.initial); result = R.resume(host)
        self.assertTrue(result['ok']); self.assertEqual(host.events.count('start'),1)
        self.assertEqual(T.T.A.strict(host.records[host.cohort_id])['previous_state'],before)
        self.assertEqual(result['budget']['total_pause_seconds'],R.LIFETIME+4)
        self.assertLessEqual(result['budget']['max_cohort_pause_seconds'],10)

    def test_uncertain_start_is_never_repeated(self):
        host = Host('start'); result = R.resume(host)
        self.assertEqual(result['start_outcome'],'unknown-inspect-only')
        self.assertEqual(host.events.count('start'),1)

    def test_detached_work_and_old_epoch_are_not_idle(self):
        host = Host(); args = [host.observe('ingress.status'),host.observe('runtime.status'),host.observe('ingress.status')]
        self.assertTrue(R.drained(*args,host.required))
        args[1]['tasks']['detached_writers']['active']=1
        self.assertFalse(R.drained(*args,host.required))
        args[1]['tasks']['detached_writers']['active']=0; args[2]['epoch']=4
        self.assertFalse(R.drained(*args,host.required))

    def test_remote_target_argument_remains_bounded_and_installer_compiles(self):
        import base64, shlex
        payload = {'support':base64.b64encode(pathlib.Path(__file__).with_name('accept_recovery_generation.py').read_bytes()).decode(),
                   'canary':base64.b64encode(pathlib.Path(__file__).with_name('recovery_generation_canary.py').read_bytes()).decode()}
        code = R.remote_code('target',payload,R.DERIVED_PROGRAM)
        self.assertLess(len(('sudo -n python3 -I -c '+shlex.quote(code)).encode()),131072)
        installer = load('optimized_installer','install_rpc_read_optimization.py')
        for script in (installer.TARGET,installer.SOURCE):compile(script,'installer','exec')

    def target_fixture(self, mutation=None):
        old = {'program_sha256':W.OLD_RPC,'app':{'control_port':18093}}
        new = {**old,'program_sha256':W.NEW_RPC}
        quiesce = {'request':{'action':'app.quiesce'},'phase':'COMPLETE','cancelled':True,
                   'instance_id':R.INSTANCE,'substeps':{k:{'phase':'COMPLETE'} for k in ('ingress','writers','redis','postgres')}}
        restore = {'request':{'action':'app.restore','op_id':'b'*32},'phase':'COMPLETE','accepted_at':10,
                   'response':{'code':'OK','epoch':24,'result':{'app':{'body':{'instance_id':R.INSTANCE}},'ingress':{'epoch':6}}}}
        ledger = {'epoch':24,'operations':{R.FAILED_OP:quiesce,'b'*32:restore}}
        if mutation:mutation(new,quiesce,restore)
        old_raw,new_raw,ledger_raw = (T.T.A.canonical(x) for x in (old,new,ledger))
        receipt = {'v':1,'run':R.RUN,'owner':R.OWNER,'old_program_sha256':W.OLD_RPC,
                   'new_program_sha256':W.NEW_RPC,'old_config_sha256':R.sha(old_raw),
                   'new_config_sha256':R.sha(new_raw),'ledger_sha256':R.sha(ledger_raw),'business_processes_unchanged':True}
        values = {'verification.json':T.T.A.canonical(receipt),'previous-config.json':old_raw,
                  'config.json':new_raw,'state.json':ledger_raw}
        proof = {'instance_id':R.INSTANCE,'ledger_epoch':24,'target_ledger_sha256':R.sha(ledger_raw),
                 'controller_config_sha256':R.sha(new_raw)}
        def load_canary(*args):
            code=args[2].decode();compile(code,'patched-canary','exec')
            self.assertIn('RPC_SHA256 = "'+W.NEW_RPC+'"',code)
            self.assertIn('receipt.get("controller_config_raw_sha256") == "'+R.sha(old_raw)+'"',code)
            return types.SimpleNamespace(target_preflight=lambda:proof)
        support=types.SimpleNamespace(strict=T.T.A.strict,root_read=lambda path:values[path.name],load_bytes=load_canary)
        with mock.patch.object(R,'OLD_CONFIG_PIN',R.sha(old_raw)), mock.patch.object(R,'TARGET_CONFIG_SHA',R.sha(new_raw)), \
             mock.patch.object(R,'TARGET_LEDGER_SHA',R.sha(ledger_raw)):
            return R.target_check(support,pathlib.Path(__file__).with_name('recovery_generation_canary.py').read_bytes())

    def test_upgrade_is_bound_to_exact_predecessor_and_same_resource_config(self):
        self.assertEqual(self.target_fixture()['restored_ingress_epoch'],6)
        for change in (lambda n,q,r:n.update(app={'control_port':18092}),
                       lambda n,q,r:q.update(cancelled=False),
                       lambda n,q,r:q['substeps']['redis'].update(phase='INTENT'),
                       lambda n,q,r:r['response']['result']['ingress'].update(epoch=4)):
            with self.assertRaises(RuntimeError):self.target_fixture(change)


if __name__ == '__main__':unittest.main()
