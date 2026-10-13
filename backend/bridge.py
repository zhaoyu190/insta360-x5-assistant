#!/usr/bin/env python3
"""Local-only read-only NFSv3 -> Insta360 OSC/HTTP Range bridge.
Protocol: RFC 1813. No third-party packages; bounded RAM and temporary disk cache.
"""
import collections
from concurrent.futures import ThreadPoolExecutor
import datetime
import http.client
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import struct
import threading
import time
from urllib.parse import quote, urlsplit

U = lambda n: struct.pack('!I', n)
Q = lambda n: struct.pack('!Q', n)

def O(data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    return U(len(data)) + data + b'\0' * (-len(data) % 4)

class XDR:
    def __init__(self, data):
        self.data, self.pos = data, 0
    def take(self, n):
        if n < 0 or self.pos + n > len(self.data):
            raise ValueError('Truncated XDR')
        result = self.data[self.pos:self.pos+n]
        self.pos += n
        return result
    def u(self): return struct.unpack('!I', self.take(4))[0]
    def q(self): return struct.unpack('!Q', self.take(8))[0]
    def opaque(self):
        n = self.u()
        data = self.take(n)
        self.take(-n % 4)
        return data

from camera_io import Camera

class Volume:
    def __init__(self, entries, backend, log):
        self.backend, self.log = backend, log
        self.salt = os.urandom(8)
        self.now = int(time.time())
        self.nodes = {1: {'name':'X5', 'dir':True, 'size':4096}}
        self.names = {}
        for index, entry in enumerate(entries, 2):
            self.nodes[index] = dict(entry, dir=False)
            self.names[entry['name']] = index
    def handle(self, index): return self.salt+Q(index)
    def node_id(self, reader):
        handle = reader.opaque()
        if len(handle) != 16 or handle[:8] != self.salt:
            raise KeyError('stale handle')
        index = struct.unpack('!Q', handle[8:])[0]
        if index not in self.nodes:
            raise KeyError('stale handle')
        return index
    def attr(self, index):
        n = self.nodes[index]
        return (U(2 if n['dir'] else 1)+U(0o555 if n['dir'] else 0o444)+U(2 if n['dir'] else 1)
            +U(os.getuid())+U(os.getgid())+Q(n['size'])+Q(n['size'])+U(0)+U(0)+Q(0x5835)+Q(index)
            +(U(self.now)+U(0))*3)
    def post(self, index): return U(1)+self.attr(index)
    def error(self, proc, status, index=None):
        post = self.post(index) if index in self.nodes else U(0)
        wcc = U(0)+post
        if proc == 1: tail = b''
        elif proc in (2,7,8,9,10,11,12,13,21): tail = wcc
        elif proc == 14: tail = wcc+wcc
        elif proc == 15: tail = post+wcc
        else: tail = post
        return U(status)+tail
    def nfs(self, proc, r):
        if proc == 0: return b''
        try:
            index = self.node_id(r)
        except KeyError:
            return self.error(proc, 70)
        node = self.nodes[index]
        post = self.post(index)
        if proc in (2,7,8,9,10,11,12,13,14,15,21):
            return self.error(proc, 30, index)
        if proc == 1: return U(0)+self.attr(index)
        if proc == 3:
            name = r.opaque().decode('utf-8')
            if not node['dir']: return self.error(proc, 20, index)
            child = 1 if name in ('.','..') else self.names.get(name)
            if child is None: return self.error(proc, 2, index)
            return U(0)+O(self.handle(child))+self.post(child)+post
        if proc == 4:
            requested = r.u()
            return U(0)+post+U(requested & (3 if node['dir'] else 1))
        if proc == 5: return self.error(proc, 22, index)
        if proc == 6:
            if node['dir']: return self.error(proc, 21, index)
            offset, count = r.q(), min(r.u(), 1024*1024)
            try:
                data = self.backend.read(node, offset, count)
            except Exception as exc:
                self.log('read_error', file=node['name'], offset=offset, error=str(exc))
                return self.error(proc, 5, index)
            return U(0)+post+U(len(data))+U(offset+len(data)>=node['size'])+O(data)
        if proc in (16,17):
            if not node['dir']: return self.error(proc, 20, index)
            cookie, verifier = r.q(), r.take(8)
            limit = r.u()
            if proc == 17: limit = min(r.u(), 1024*1024)
            if cookie and verifier != self.salt: return self.error(proc, 10003, index)
            children = [('.',1),('..',1)] + list(self.names.items())
            result = U(0)+post+self.salt
            position = min(cookie, len(children))
            while position < len(children):
                name, child = children[position]
                entry = U(1)+Q(child)+O(name)+Q(position+1)
                if proc == 17: entry += self.post(child)+U(1)+O(self.handle(child))
                if len(result)+len(entry)+8 > limit:
                    if position == cookie: return self.error(proc, 10005, index)
                    break
                result += entry
                position += 1
            return result+U(0)+U(position>=len(children))
        if proc == 18:
            size = sum(n['size'] for n in self.nodes.values())
            return U(0)+post+Q(size)+Q(0)+Q(0)+Q(len(self.nodes))+Q(0)+Q(0)+U(0)
        if proc == 19:
            return U(0)+post+U(1048576)+U(262144)+U(4096)+U(1048576)+U(262144)+U(4096)+U(32768)+Q(2**63-1)+U(1)+U(0)+U(8)
        if proc == 20:
            return U(0)+post+U(1)+U(255)+U(1)+U(1)+U(0)+U(1)
        return self.error(proc, 10004, index)
    def rpc(self, packet):
        r = XDR(packet)
        xid, kind, rpcvers, prog, vers, proc = [r.u() for _ in range(6)]
        reply = U(xid)+U(1)+U(0)+U(0)+O(b'')
        if kind != 0 or rpcvers != 2: raise ValueError('Unsupported RPC')
        for _ in range(2): r.u(); r.opaque()
        if prog not in (100003,100005): return reply+U(1)
        if vers != 3: return reply+U(2)+U(3)+U(3)
        if (prog == 100003 and proc > 21) or (prog == 100005 and proc > 5):
            return reply+U(3)
        try:
            if prog == 100003:
                result = self.nfs(proc, r)
            elif proc == 1:
                path = r.opaque()
                result = U(0)+O(self.handle(1))+U(1)+U(1) if path in (b'/',b'/X5') else U(2)
            elif proc == 2: result = U(0)
            elif proc == 5: result = U(1)+O('/X5')+U(0)+U(0)
            else: result = b''
            self.log('rpc', program=prog, procedure=proc)
            return reply+U(0)+result
        except (ValueError, UnicodeError, struct.error) as exc:
            self.log('rpc_error', error=str(exc), procedure=proc)
            return reply+U(4)

class Handler(socketserver.BaseRequestHandler):
    def send_reply(self, packet):
        try:
            reply = self.server.volume.rpc(packet)
            with self.reply_lock:
                self.request.sendall(U(0x80000000 | len(reply))+reply)
        except (ConnectionError, TimeoutError, OSError):
            pass
    def read_reply(self, packet):
        try: self.send_reply(packet)
        finally: self.server.read_slots.release()
    def recv_exact(self, size):
        result = bytearray()
        while len(result) < size:
            piece = self.request.recv(size-len(result))
            if not piece: raise EOFError()
            result.extend(piece)
        return bytes(result)
    def handle(self):
        self.reply_lock = threading.Lock()
        self.request.settimeout(120)
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            while True:
                packet = bytearray()
                while True:
                    marker = struct.unpack('!I', self.recv_exact(4))[0]
                    size = marker & 0x7fffffff
                    if len(packet)+size > 2*1024*1024: raise ValueError('RPC too large')
                    packet.extend(self.recv_exact(size))
                    if marker & 0x80000000: break
                # macOS pipelines RPCs on a single TCP connection. A camera read
                # must not hold up file existence/metadata checks used by Studio.
                if len(packet)>=24 and struct.unpack_from('!I',packet,12)[0]==100003 and struct.unpack_from('!I',packet,20)[0]==6:
                    self.server.read_slots.acquire()
                    try:self.server.read_workers.submit(self.read_reply,bytes(packet))
                    except Exception:
                        self.server.read_slots.release();raise
                else:self.send_reply(bytes(packet))
        except (EOFError, ConnectionError, TimeoutError, ValueError, OSError):
            pass

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    def __init__(self,*args,**kwargs):
        self.read_workers=ThreadPoolExecutor(max_workers=4,thread_name_prefix='x5-nfs-read')
        self.read_slots=threading.BoundedSemaphore(8)
        super().__init__(*args,**kwargs)
    def server_close(self):
        super().server_close()
        self.read_workers.shutdown(wait=True)

class Log:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / (datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'.jsonl')
        self.file = self.path.open('a', encoding='utf-8', buffering=1)
        self.lock = threading.Lock()
    def __call__(self, event, **values):
        with self.lock:
            self.file.write(json.dumps(dict(time=datetime.datetime.now().astimezone().isoformat(), event=event, **values), ensure_ascii=False)+'\n')

if __name__ == '__main__':
    raise SystemExit('请使用「连接相机.command」启动。')
