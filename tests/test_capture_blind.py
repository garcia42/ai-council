import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from operations.capture_blind import launch

class BlindLaunchTest(unittest.TestCase):
    def test_verbatim_prompt_binding_and_probe(self):
        with tempfile.TemporaryDirectory(dir='/var/tmp') as temp:
            root=Path(temp); prompt=root/'prompt.md'; prompt.write_bytes(b'exact prompt\n')
            digest=hashlib.sha256(prompt.read_bytes()).hexdigest(); calls=[]
            def run(argv,**kwargs):
                calls.append((argv,kwargs)); target=Path(argv[argv.index('-o')+1])
                target.write_text('NO INFORMATION' if len(calls)==1 else json.dumps({'capture':{'inputArtifactSha256':digest}}))
                return subprocess.CompletedProcess(argv,0,b'model: test\n')
            launch(prompt,digest,root/'answer',run=run)
            self.assertEqual(calls[1][1]['input'],prompt.read_bytes())
            self.assertIn('.codexhome-blind',calls[1][1]['env']['CODEX_HOME'])
            self.assertTrue(json.loads((root/'answer/provenance.json').read_text())['verbatimInput'])
    def test_changed_prompt_refuses_before_provider(self):
        with tempfile.TemporaryDirectory(dir='/var/tmp') as temp:
            p=Path(temp)/'prompt'; p.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'changed'):
                launch(p,'0'*64,Path(temp)/'answer',run=lambda *a,**k:self.fail('provider called'))
    def test_contaminated_probe_does_not_launch_review(self):
        with tempfile.TemporaryDirectory(dir='/var/tmp') as temp:
            p=Path(temp)/'prompt'; p.write_bytes(b'input'); calls=[]
            def run(argv,**kwargs):
                calls.append(argv); Path(argv[argv.index('-o')+1]).write_text('I know the project')
                return subprocess.CompletedProcess(argv,0,b'')
            with self.assertRaisesRegex(ValueError,'isolation'):
                launch(p,hashlib.sha256(p.read_bytes()).hexdigest(),Path(temp)/'answer',run=run)
            self.assertEqual(len(calls),1)
