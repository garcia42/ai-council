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


def permission_args(prompt_path):
    # The helper executable and its argv0 shim must survive the Linux sandbox.
    fs={':minimal':'read',str(prompt_path):'read',
        str(Path(CODEX).resolve()):'read',BLIND_HOME+'/tmp/arg0':'read',
        '/proc':'deny'}
    return ['-c','default_permissions="capture_blind"',
            '-c','permissions.capture_blind.filesystem={'+','.join(json.dumps(k)+'='+json.dumps(v) for k,v in fs.items())+'}',
            '-c','permissions.capture_blind.network.enabled=false',
            '-c','approval_policy="never"','-c','web_search="disabled"',
            '-c','shell_environment_policy.inherit="none"']


def verify_boundary(prompt_path, digest, work, output, env, run):
    adjacent=Path(work)/'not-for-seat'; adjacent.write_text('negative-access-fixture')
    script="""import hashlib,json,os,pathlib,socket,sys
p=pathlib.Path(sys.argv[1]); assert hashlib.sha256(p.read_bytes()).hexdigest()==sys.argv[2]
for target in (sys.argv[3], '/home/trader/CLAUDE.md', sys.argv[4], sys.argv[5]):
    try:
        handle=open(target,'rb')
    except (PermissionError,FileNotFoundError):
        pass
    else:
        handle.close(); raise AssertionError('forbidden file accessible')
try:
    socket.create_connection(('127.0.0.1',9),timeout=1)
except PermissionError:
    pass
else:
    raise AssertionError('network boundary failed')
print(json.dumps({'promptReadable':True,'unrelatedFilesDenied':True,'credentialsDenied':True,'hostProcessDenied':True,'networkDenied':True}))
"""
    result=run([CODEX,'sandbox','-P','capture_blind',*permission_args(prompt_path),'-C',work,
                '/usr/bin/python3','-c',script,str(prompt_path),digest,str(adjacent),BLIND_HOME+'/auth.json',f'/proc/{os.getpid()}/environ'],
               stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env,timeout=30)
    (output/'boundary-test.txt').write_bytes(result.stdout)
    if result.returncode:
        raise ValueError('blind access boundary failed before provider launch')
    checks=json.loads(result.stdout)
    if len(checks)!=5 or not all(value is True for value in checks.values()):
        raise ValueError('blind boundary proof incomplete')


def launch(prompt_path, prompt_sha256, output_directory, *, run=subprocess.run):
    prompt_path=Path(prompt_path).resolve(strict=True)
    prompt=prompt_path.read_bytes()
    if hashlib.sha256(prompt).hexdigest()!=prompt_sha256:
        raise ValueError('retained prompt changed')
    output=Path(output_directory).absolute()
    output.mkdir(mode=0o700)  # Never reuse a namespace.
    env={'PATH':os.defpath,'HOME':BLIND_HOME,'CODEX_HOME':BLIND_HOME,'LANG':'C.UTF-8'}
    with tempfile.TemporaryDirectory(prefix='council-v2-blind-',dir='/var/tmp') as work:
        verify_boundary(prompt_path,prompt_sha256,work,output,env,run)
        def invoke(content, name, timeout):
            argv=[CODEX,'exec','--ephemeral','--ignore-user-config','--ignore-rules','--skip-git-repo-check',*permission_args(prompt_path),'-C',work,'-o',str(output/(name+'.txt')),'-']
            try:
                result=run(argv,input=content,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env,timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                (output/(name+'-transcript.txt')).write_bytes(exc.stdout or b'')
                raise
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
        'accessBoundaryTestPassed':True,'transcriptInspected':None,
        'modelIdentitySource':'answer-transcript.txt launcher header; retain actual model ID in seatResults',
        'toolPolicy':'Minimal system files and exact prompt readable; unrelated files, credentials, host processes and command network denied. Private sandbox proc remains. Inspect transcript before accepting independence.',
        'answerSha256':hashlib.sha256((output/'answer.txt').read_bytes()).hexdigest()},indent=2)+'\n')
    return value


if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--prompt',required=True); p.add_argument('--sha256',required=True); p.add_argument('--output-directory',required=True)
    a=p.parse_args(); os.umask(0o077); launch(a.prompt,a.sha256,a.output_directory)
