import json,os,subprocess,sys,tempfile,threading
from pathlib import Path

if sys.platform != 'darwin' or os.environ.get('YINGSHI_RUN_NFS_TESTS') != '1':
    raise SystemExit('Native mounts are opt-in: set YINGSHI_RUN_NFS_TESTS=1 on macOS.')

source=Path(__file__).resolve().parents[1]/'backend'
worker='''
import sys,subprocess
sys.path.insert(0,sys.argv[1])
from pathlib import Path
import app_backend as b
class FakeCamera:
 def __init__(self,*a,**kw):
  self.bytes_received=self.requests=self.cache_hits=self.disk_hits=self.disk_bytes=0
 def catalog(self):return [{'name':'TEST.txt','url':'/TEST.txt','size':100}]
 def benchmark(self,entries):pass
 def read(self,node,offset,count):return b'x'*min(count,max(0,100-offset))
 def close(self):pass
b.Camera=FakeCamera
class FakeLive:
 def __init__(self,*args):pass
 def connect_session(self):pass
 def sync_status(self):pass
 def sync_modes(self):pass
 def tick(self):pass
 def stop_preview(self):pass
 def close(self):pass
b.LiveCamera=FakeLive
real=b.system
first=True
def system(command):
 global first
 if command[0]=='/sbin/umount' and first:
  first=False
  return subprocess.CompletedProcess(command,1,'','Resource busy')
 return real(command)
b.system=system
b.run(Path(sys.argv[2]))
'''
with tempfile.TemporaryDirectory(prefix='yingshi-lifecycle-test-') as temp:
 p=subprocess.Popen([sys.executable,'-B','-c',worker,str(source),str(Path(temp).resolve())],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
 timer=threading.Timer(30,p.kill);timer.start()
 events=[]
 try:
  for line in p.stdout:
   event=json.loads(line);events.append(event['event'])
   if event['event']=='connected':
    root=Path(event['path']);assert (root/'TEST.txt').read_bytes()==b'x'*100
    p.stdin.write('{"command":"disconnect"}\n');p.stdin.flush()
   elif event['event']=='busy':
    p.stdin.write('{"command":"disconnect"}\n');p.stdin.flush()
  p.wait(timeout=5)
  assert p.returncode==0,(p.returncode,p.stderr.read(),events)
  assert 'connected' in events and 'busy' in events and 'disconnected' in events,events
  assert not os.path.ismount(Path(temp)/'相机素材')
  print('PASS: app backend connects, retains service on busy volume, retries disconnect, unmounts, exits')
 finally:
  timer.cancel()
  if p.poll() is None:p.kill()
