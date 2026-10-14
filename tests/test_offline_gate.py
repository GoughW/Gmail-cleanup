"""Independent guard test, including Python child-process inheritance."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class OfflineGateTests(unittest.TestCase):
    def test_guard_blocks_external_dns_and_connections_in_children_and_removes_bindings(self):
        root=Path(__file__).resolve().parents[1]
        environment=dict(os.environ)
        environment['PYTHONPATH']=str(root/'tests/network_guard')
        environment['GMAIL_REFRESH_TOKEN']='LOCAL-FAKE-NEVER-USE'
        source='''
import os,socket
assert 'GMAIL_REFRESH_TOKEN' not in os.environ
for host in ('gmail.googleapis.com','www.googleapis.com','oauth2.googleapis.com'):
    try:socket.getaddrinfo(host,443)
    except AssertionError:pass
    else:raise AssertionError('External DNS was permitted')
for method in ('connect','connect_ex'):
    connection=socket.socket()
    try:
        try:getattr(connection,method)(('192.0.2.1',443))
        except AssertionError:pass
        else:raise AssertionError('External connection was permitted')
    finally:connection.close()
print('loopback-only guard active')
'''
        result=subprocess.run([sys.executable,'-c',source],env=environment,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'loopback-only guard active')
