import contextlib,hashlib,importlib.util,io,json,os,socket,socketserver,struct,sys,tempfile,threading,time,unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from camera_io import Camera,RangeResponseError
from bridge import Volume,Server,Handler,U,O,Q

SIZE=6*1024**3+127
PATTERN=bytes(range(251))
def content(offset,count):
 start=offset%len(PATTERN)
 return (PATTERN*((count+start)//len(PATTERN)+1))[start:start+count]
class QuietHTTP(ThreadingHTTPServer):
 daemon_threads=True
 def handle_error(self,*args):pass
class CameraHTTP(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*args):pass
 def do_GET(self):
  if self.path=='/osc/info':return self.json({'model':'Insta360 X5'})
  lo,hi=map(int,self.headers['Range'][6:].split('-'));hi=min(hi,SIZE-1)
  with self.server.guard:
   self.server.requests.append((self.path,lo,hi,self.client_address[1]))
   attempt=sum(p==self.path and start==lo for p,start,_,_ in self.server.requests)
  if self.path=='/transient' and attempt==1:
   self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
  body=content(lo,hi-lo+1)
  self.send_response(206)
  self.send_header('Content-Length',str(len(body)))
  self.send_header('Content-Range',f'bytes {lo+1 if self.path=="/wrong" else lo}-{hi}/{SIZE}')
  self.send_header('Connection','keep-alive');self.end_headers()
  if self.path=='/short' and attempt==1:
   self.wfile.write(body[:max(1,len(body)//2)]);self.wfile.flush()
   self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
  # Emulate observed camera firmware: bytes remain after declared response body.
  self.wfile.write(body+b'private-video-payload-not-an-http-header\n'*30000)
  self.wfile.flush();self.close_connection=False
 def json(self,data):
  body=json.dumps(data).encode();self.send_response(200);self.send_header('Content-Length',str(len(body)))
  self.end_headers();self.wfile.write(body)
 def do_POST(self):
  payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  if self.path=='/osc/state':return self.json({'state':{}})
  offset=payload['parameters']['startPosition']
  names=[f'{prefix}_20261001_12000{i}_{lens}_00{i}.{suffix}' for i in range(3) for prefix,lens,suffix in [('VID','00','insv'),('LRV','01','lrv')]]
  files=[{'fileUrl':'/DCIM/Camera01/'+n} for n in names[offset:offset+2]]
  self.json({'state':'done','results':{'entries':files,'totalEntries':2}})
class TestRepair(unittest.TestCase):
 def setUp(self):
  self.http=QuietHTTP(('127.0.0.1',0),CameraHTTP);self.http.requests=[];self.http.guard=threading.Lock()
  threading.Thread(target=self.http.serve_forever,daemon=True).start()
  self.events=[];self.camera=Camera(lambda event,**kw:self.events.append((event,kw)),'127.0.0.1',self.http.server_port)
 def tearDown(self):self.camera.close();self.http.shutdown();self.http.server_close()
 def test_old_connection_reuse_reproduces_error_new_reader_recovers(self):
  # A naive persistent HTTP client reproduces the legacy desynchronization.
  import http.client
  connection=http.client.HTTPConnection('127.0.0.1',self.http.server_port,timeout=2)
  try:
   connection.request('GET','/poison',headers={'Range':'bytes=0-15','Connection':'keep-alive'})
   response=connection.getresponse()
   self.assertEqual(response.read(16),content(0,16))
   connection.request('GET','/poison',headers={'Range':'bytes=100-115','Connection':'keep-alive'})
   with self.assertRaises(http.client.HTTPException):connection.getresponse()
  finally:connection.close()
  for start in [0,100,4*1024**3+113,0,123456]:
   self.assertEqual(self.camera.request('/poison',span=(start,start+1023))[0],content(start,1024))
 def test_retry_truncated_and_disconnected_reads_no_duplicate_data(self):
  for path in ['/transient','/short']:
   self.assertEqual(self.camera.request(path,span=(18,4113))[0],content(18,4096))
   self.assertEqual(sum(p==path for p,*_ in self.http.requests),2)
  self.assertEqual(self.camera.read_retries,2)
 def test_mismatched_ranges_never_cached(self):
  with self.assertRaises(RangeResponseError):self.camera.request('/wrong',span=(0,31))
  self.assertEqual(len(self.http.requests),1)
  self.assertFalse(self.camera.cache)
 def test_catalog_keeps_original_proxy_pairs(self):
  raw=self.camera.request
  def request(url,*args,**kwargs):
   if 'raw_limit' in kwargs:raise RangeResponseError('403')
   return raw(url,*args,**kwargs)
  self.camera.request=request
  with contextlib.redirect_stdout(io.StringIO()):entries=self.camera.catalog()
  self.assertEqual(len(entries),6)
  summary=next(kw for ev,kw in self.events if ev=='catalog_complete')
  self.assertEqual(summary['failed_files'],0);self.assertEqual(summary['missing_proxies'],[])
 def test_export_style_sequential_read_checksums_and_random_seek(self):
  node={'url':'/poison','size':SIZE}
  with ThreadPoolExecutor(max_workers=3) as workers:
   futures=[workers.submit(self.camera.read,node,start,2*1024**2+11) for start in [0,4*1024**3+99,SIZE-3*1024**2]]
   for start,future in zip([0,4*1024**3+99,SIZE-3*1024**2],futures):
    self.assertEqual(hashlib.sha256(future.result()).digest(),hashlib.sha256(content(start,2*1024**2+11)).digest())
  actual=hashlib.sha256();expected=hashlib.sha256()
  for start in range(0,24*1024**2,256*1024):
   actual.update(self.camera.read(node,start,256*1024));expected.update(content(start,256*1024))
  self.assertEqual(actual.digest(),expected.digest())
  self.assertEqual(self.camera.read(node,SIZE-127,300),content(SIZE-127,127))
  self.assertEqual(self.camera.read(node,SIZE,10),b'')

def rpc(xid,proc,args):return U(xid)+U(0)+U(2)+U(100003)+U(3)+U(proc)+U(0)+O(b'')+U(0)+O(b'')+args
def recv_rpc(sock):
 def exact(n):
  data=b''
  while len(data)<n:data+=sock.recv(n-len(data))
  return data
 n=struct.unpack('!I',exact(4))[0]&0x7fffffff
 return exact(n)
class TestNFS(unittest.TestCase):
 def test_metadata_does_not_wait_for_camera_read(self):
  blocked=threading.Event();started=threading.Event()
  class Slow:
   def read(self,node,offset,count):started.set();blocked.wait(3);return b'x'*count
  server=Server(('127.0.0.1',0),Handler);server.volume=Volume([{'name':'test.insv','size':100,'url':'/test'}],Slow(),lambda *a,**kw:None)
  threading.Thread(target=server.serve_forever,daemon=True).start()
  sock=socket.create_connection(server.server_address);sock.settimeout(1)
  try:
   a=rpc(101,6,O(server.volume.handle(2))+Q(0)+U(10))
   sock.sendall(U(0x80000000|len(a))+a);self.assertTrue(started.wait(1))
   b=rpc(102,1,O(server.volume.handle(2)));sock.sendall(U(0x80000000|len(b))+b)
   self.assertEqual(struct.unpack('!I',recv_rpc(sock)[:4])[0],102)
   blocked.set();self.assertEqual(struct.unpack('!I',recv_rpc(sock)[:4])[0],101)
  finally:
   blocked.set();sock.close();server.shutdown();server.server_close()
if __name__=='__main__':unittest.main(verbosity=2)
