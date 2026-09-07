#!/usr/bin/env python3
"""Send a retained V2 prompt verbatim to an isolated Codex seat, without wrapping it."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

CODEX = '/home/trader/.local/bin/codex'
BLIND_HOME = '/home/trader/.claude/skills/blind-seat/.codexhome-blind'
PROBE = 'Answer in under 15 words, without using any tools: what trading system, if any, is associated with the path /home/trader? If you have no information, reply exactly: NO INFORMATION.'


def launch(prompt_path, prompt_sha256, output_directory, *, run=subprocess.run):
    prompt_path=Path(prompt_path).resolve(strict=True)
    prompt=prompt_path.read_bytes()
    if hashlib.sha256(prompt).hexdigest()!=prompt_sha256:
        raise ValueError('retained prompt changed')
    output=Path(output_directory).absolute()
    output.mkdir(mode=0o700)  # Never reuse a namespace.
    env={**os.environ,'CODEX_HOME':BLIND_HOME}
    with tempfile.TemporaryDirectory(prefix='council-v2-blind-',dir='/var/tmp') as work:
        def invoke(content, name, timeout):
            argv=[CODEX,'exec','--ephemeral','--skip-git-repo-check','-s','read-only','-C',work,'-o',str(output/(name+'.txt')),'-']
            result=run(argv,input=content,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env,timeout=timeout)
            (output/(name+'-transcript.txt')).write_bytes(result.stdout)
            if result.returncode:
                raise ValueError('isolated provider did not complete')
            return (output/(name+'.txt')).read_text().strip()
        probe=invoke(PROBE.encode(),'probe',90)
        if probe not in ('NO INFORMATION','NO INFORMATION.'):
            raise ValueError('blindness probe did not establish isolation')
        answer=invoke(prompt,'answer',600)
    if prompt_path.read_bytes()!=prompt:
        raise ValueError('retained prompt changed during launch')
    value=json.loads(answer)
    if value.get('capture',{}).get('inputArtifactSha256')!=prompt_sha256:
        raise ValueError('seat did not bind its retained prompt')
    (output/'provenance.json').write_text(json.dumps({'provider':'openai-codex','inputArtifactSha256':prompt_sha256,
        'promptFile':str(prompt_path),'verbatimInput':True,'blindnessProbePassed':True,
        'modelIdentitySource':'answer-transcript.txt launcher header; retain actual model ID in seatResults',
        'toolPolicy':'Only hash the named retained prompt; inspect transcript before accepting independence',
        'answerSha256':hashlib.sha256((output/'answer.txt').read_bytes()).hexdigest()},indent=2)+'\n')
    return value


if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--prompt',required=True); p.add_argument('--sha256',required=True); p.add_argument('--output-directory',required=True)
    a=p.parse_args(); os.umask(0o077); launch(a.prompt,a.sha256,a.output_directory)
