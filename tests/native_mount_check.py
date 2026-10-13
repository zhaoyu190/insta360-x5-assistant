import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
import contextlib,subprocess,tempfile,threading,os
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from bridge import Volume,Server,Handler
from camera_io import Camera

if sys.platform != 'darwin' or os.environ.get('YINGSHI_RUN_NFS_TESTS') != '1':
    raise SystemExit('Native mounts are opt-in: set YINGSHI_RUN_NFS_TESTS=1 on macOS.')

size=6*1024**3
class HTTP(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*a):pass
    def do_GET(self):
        lo,hi=map(int,self.headers['Range'][6:].split('-'));hi=min(hi,size-1)
        count=hi-lo+1
        self.send_response(206);self.send_header('Content-Range',f'bytes {lo}-{hi}/{size}')
        self.send_header('Content-Length',str(count));self.end_headers()
        data=(bytes(range(256))*((count+255)//256+1))[lo%256:lo%256+count]
        self.wfile.write(data)
http=ThreadingHTTPServer(('127.0.0.1',0),HTTP)
threading.Thread(target=http.serve_forever,daemon=True).start()
with tempfile.TemporaryDirectory(prefix='yingshi-nfs-test-') as tmp:
    camera=Camera(lambda *a,**k:None,'127.0.0.1',http.server_port,Path(tmp)/'cache')
    mount=Path(tmp).resolve()/'mount';mount.mkdir()
    server=Server(('127.0.0.1',0),Handler)
    server.volume=Volume([{'name':'synthetic.insv','size':size,'url':'/DCIM/Camera01/test.insv'}],camera,lambda *a,**k:None)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    port=server.server_address[1]
    try:
        options=f'vers=3,tcp,port={port},mountport={port},ro,soft,locallocks,noacl,nobrowse,rsize=1048576,readahead=4,timeo=100,retrans=3,retrycnt=0'
        subprocess.run(['/sbin/mount_nfs','-o',options,'127.0.0.1:/X5',str(mount)],check=True,timeout=20)
        p=mount/'synthetic.insv';assert p.stat().st_size==size
        with p.open('rb') as f:
            for offset in (0,4*1024**3+123,1024**2-127,size-1024):
                count=min(1024**2+4096,size-offset)
                f.seek(offset);actual=f.read(count)
                expected=(bytes(range(256))*((count+255)//256+1))[offset%256:offset%256+count]
                assert actual==expected,(offset,len(actual))
        print('PASS: native macOS mount -> concurrent NFS -> bounded cache -> isolated HTTP; 6 GiB size, >4 GiB seeks, block boundaries, EOF')
        print('Network bytes read:',camera.bytes_received)
    finally:
        if os.path.ismount(mount):subprocess.run(['/sbin/umount',str(mount)],check=True,timeout=20)
        server.shutdown();server.server_close();camera.close();http.shutdown();http.server_close()
