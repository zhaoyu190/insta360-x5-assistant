import base64,io,json,socket,struct,sys,threading,time,unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from fractions import Fraction
from pathlib import Path
from PIL import Image
import av
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from live_camera import LiveCamera, PreviewDecoder, fields


def fixture():
 codec=av.CodecContext.create('libx264','w');codec.width=320;codec.height=160
 codec.pix_fmt='yuv420p';codec.time_base=Fraction(1,30)
 codec.options={'preset':'ultrafast','tune':'zerolatency'}
 data=b''
 for i in range(5):
  frame=av.VideoFrame.from_image(Image.new('RGB',(320,160),(30,140,180)))
  frame.pts=i
  data+=b''.join(bytes(p) for p in codec.encode(frame))
 return data+b''.join(bytes(p) for p in codec.encode(None))

class CameraStub:
 def __init__(self):
  self.codes=[];self.http_commands=[];self.state=0;self.reject=False;self.drop_start=False
  self.listener=socket.socket();self.listener.bind(('127.0.0.1',0));self.listener.listen()
  self.port=self.listener.getsockname()[1];self.video=fixture()
  threading.Thread(target=self.serve,daemon=True).start()
  parent=self
  class Handler(BaseHTTPRequestHandler):
   def do_POST(self):
    data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
    name=data.get('name');parent.http_commands.append(name)
    if name=='camera.startCapture':parent.state=1
    elif name=='camera.stopCapture':parent.state=0
    if parent.drop_start and name=='camera.startCapture':self.connection.close();return
    body=json.dumps({'state':'error','error':{'message':'SD card full'}} if parent.reject else {'state':'done','results':{}}).encode()
    self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
   def log_message(self,*args):pass
  self.http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
  threading.Thread(target=self.http.serve_forever,daemon=True).start()
 def serve(self):
  try:
   self.client,_=self.listener.accept();buffer=b''
   while True:
    part=self.client.recv(65536)
    if not part:return
    buffer+=part
    while len(buffer)>=4:
     size=int.from_bytes(buffer[:4],'little')
     if len(buffer)<size:break
     packet=buffer[4:size];buffer=buffer[size:]
     if packet[:3]!=b'\x04\x00\x00':continue
     code=int.from_bytes(packet[3:5],'little');self.codes.append(code)
     status=500 if self.reject else 200
     payload=b'\x12\x04busy' if self.reject else (b'\x0a\x02\x08'+bytes([self.state]) if code==15 else b'')
     response=b'\x04\x00\x00'+struct.pack('<H',status)+packet[5:12]+payload
     self.send(response)
     if code==1 and not self.reject:
      # Tiny TCP fragments exercise both packet and H264 reassembly.
      for p in range(0,len(self.video),257):
       self.send(b'\x01\x00\x00\x20'+b'\x00'*8+self.video[p:p+257])
  except OSError:pass
 def send(self,payload):
  data=struct.pack('<I',len(payload)+4)+payload
  self.client.sendall(data[:2]);self.client.sendall(data[2:11]);self.client.sendall(data[11:])
 def close(self):
  self.listener.close();self.http.shutdown();self.http.server_close()
  if hasattr(self,'client'):self.client.close()

class Tests(unittest.TestCase):
 def setUp(self):
  self.stub=CameraStub();self.events=[]
  self.camera=LiveCamera(lambda event,**kw:self.events.append((event,kw)),lambda *a,**kw:None,
   host='127.0.0.1',tcp_port=self.stub.port,http_port=self.stub.http.server_port)
 def tearDown(self):self.camera.close();self.stub.close()
 def test_preview_and_recording(self):
  self.camera.sync_status();self.assertEqual(self.camera.recording,'idle')
  self.assertFalse(self.stub.http_commands)
  self.camera.start_preview()
  deadline=time.monotonic()+3
  while not any(e=='preview_frame' for e,_ in self.events) and time.monotonic()<deadline:time.sleep(.02)
  frames=[v for e,v in self.events if e=='preview_frame'];self.assertTrue(frames)
  im=Image.open(io.BytesIO(base64.b64decode(frames[0]['jpeg'])))
  self.assertEqual(im.size,(320,160));self.assertGreater(im.getpixel((50,50))[1],100)
  self.camera.start_recording();self.assertEqual(self.camera.recording,'recording')
  self.camera.stop_preview();self.assertEqual(self.stub.state,1)
  self.camera.stop_recording();self.assertEqual(self.camera.recording,'idle')
  self.assertEqual(self.stub.http_commands,['camera.setOptions','camera.startCapture','camera.stopCapture'])
 def test_reject_and_existing_capture(self):
  self.stub.state=1
  with self.assertRaises(RuntimeError):self.camera.start_recording()
  self.assertFalse(self.stub.http_commands)
  self.stub.reject=True
  with self.assertRaisesRegex(RuntimeError,'busy'):self.camera.command(15)
  with self.assertRaises(RuntimeError):self.camera.stop_recording()
  self.assertEqual(self.camera.recording,'unknown')
 def test_lost_record_ack_not_retried(self):
  self.stub.drop_start=True
  with self.assertRaises(Exception):self.camera.start_recording()
  self.assertEqual(self.stub.http_commands.count('camera.startCapture'),1)
  self.assertEqual(self.camera.recording,'unknown')
 def test_unknown_protobuf(self):
  with self.assertRaises(ValueError):fields(b'\x0a\xff')
  with self.assertRaises(ValueError):fields(b'\x0a\x03a')

if __name__=='__main__':unittest.main(verbosity=2)
