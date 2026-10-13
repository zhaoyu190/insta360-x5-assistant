import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
import contextlib
import io
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from camera_io import Camera

class Tests(unittest.TestCase):
    def camera(self,**kw):
        camera=Camera(lambda *a,**k:None,**kw)
        self.addCleanup(camera.close)
        return camera
    def test_inaccurate_total_and_index_additional_day(self):
        camera=self.camera();calls=[]
        def command(parameters):
            calls.append(parameters)
            offset=parameters['startPosition']
            names=['VID_20260922_00.insv'] if offset==0 else ['VID_20261001_00.insv'] if offset==1 else []
            return {'entries':[{'fileUrl':'/DCIM/Camera01/'+n} for n in names],'totalEntries':1}
        def request(url,payload=None,span=None,raw_limit=None):
            if url=='/osc/info':return {'model':'Insta360 X5'}
            if url=='/osc/state':return {'state':{'storageUri':'/DCIM/Camera01/'}}
            if span:return b'x',1000
            if url.endswith('fileinfo_list.list'):
                return b'\x00/DCIM/Camera01/VID_20261002_00.insv\x00'
            return b'<html><a href="http://evil.example/steal.insv">bad</a></html>'
        camera.command=command;camera.request=request
        with contextlib.redirect_stdout(io.StringIO()):entries=camera.catalog()
        self.assertEqual({e['name'] for e in entries},{'VID_20260922_00.insv','VID_20261001_00.insv','VID_20261002_00.insv'})
        self.assertEqual(len(calls),9)
    def test_ignored_pagination_stops(self):
        camera=self.camera();calls=[]
        def command(parameters):
            calls.append(parameters)
            return {'entries':[{'fileUrl':'/DCIM/Camera01/VID_20261001.insv'}],'totalEntries':99999}
        camera.command=command
        def request(url,payload=None,span=None,raw_limit=None):
            if url=='/osc/info':return {'model':'Insta360 X5'}
            if url=='/osc/state':return {'state':{}}
            if span:return b'x',100
            raise IOError('404')
        camera.request=request
        with contextlib.redirect_stdout(io.StringIO()):entries=camera.catalog()
        self.assertEqual(len(entries),1);self.assertEqual(len(calls),6)
    def test_disk_budget_reuses_evicted_memory_block_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as directory:
            camera=self.camera(cache_dir=directory);camera.block=16;camera.cache_limit=1;camera.disk_limit=32
            calls=[]
            def request(url,span):
                calls.append(span);return bytes(range(span[0],span[1]+1)),100
            camera.request=request
            node={'url':'/DCIM/Camera01/test.insv','size':100}
            self.assertEqual(camera.read(node,0,1),b'\0')
            self.assertEqual(camera.read(node,48,1),bytes([48]))
            self.assertEqual(camera.read(node,0,1),b'\0')
            self.assertEqual(len(calls),2)
            self.assertEqual(camera.read(node,96,1),bytes([96]))
            self.assertLessEqual(sum(p.stat().st_size for p in Path(directory).glob('*.blk')),32)
            self.assertGreater(camera.disk_hits,0)
            camera.close();self.assertEqual(list(Path(directory).iterdir()),[])
    def test_concurrent_reads_keep_offsets_correct(self):
        camera=self.camera();camera.block=16
        active=0;peak=0;guard=threading.Lock()
        def request(url,span):
            nonlocal active,peak
            with guard:active+=1;peak=max(peak,active)
            time.sleep(.02)
            with guard:active-=1
            return bytes(range(span[0],span[1]+1)),128
        camera.request=request
        node={'url':'/DCIM/Camera01/test.insv','size':128}
        results={}
        threads=[threading.Thread(target=lambda x:results.update({x:camera.read(node,x,4)}),args=(x,)) for x in (0,64)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(peak,2)
        for offset,data in results.items():self.assertEqual(data,bytes(range(offset,offset+4)))

class HTTPTests(unittest.TestCase):
    def test_isolated_connections_and_refusing_full_response(self):
        ports=[]
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*a):pass
            def do_GET(self):
                ports.append(self.client_address[1])
                if self.path=='/ignored':
                    self.send_response(200);self.send_header('Content-Length','999999999');self.end_headers();return
                lo,hi=map(int,self.headers['Range'][6:].split('-'))
                self.send_response(206);self.send_header('Content-Range',f'bytes {lo}-{hi}/1000')
                self.send_header('Content-Length',str(hi-lo+1));self.end_headers();self.wfile.write(b'x'*(hi-lo+1))
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        camera=Camera(lambda *a,**k:None,'127.0.0.1',server.server_port)
        try:
            self.assertEqual(camera.request('/video',span=(0,15)),(b'x'*16,1000))
            self.assertEqual(camera.request('/video',span=(32,47)),(b'x'*16,1000))
            self.assertNotEqual(ports[0],ports[1])
            with self.assertRaises(ValueError):camera.request('/ignored',span=(0,15))
        finally:
            camera.close();server.shutdown();server.server_close()

if __name__=='__main__':unittest.main(verbosity=2)
