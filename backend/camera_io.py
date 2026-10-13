"""Bounded camera catalog discovery and cached, pipelined range reads."""
import collections
from concurrent.futures import ThreadPoolExecutor
import hashlib
from html.parser import HTMLParser
import http.client
import json
from pathlib import Path
import queue
import re
import threading
import time
from urllib.parse import quote, unquote, urljoin, urlsplit

class RangeResponseError(ValueError):
    pass

def read_error_text(exc):
    # BadStatusLine may contain private video bytes; never put its body in logs.
    if isinstance(exc, (http.client.BadStatusLine, http.client.LineTooLong)):
        return 'HTTP 响应错位（已丢弃连接）'
    if isinstance(exc, http.client.IncompleteRead):
        return 'HTTP 分段提前结束'
    return str(exc)[:200]

MEDIA = ('.insv','.insp','.lrv','.mp4','.jpg','.jpeg','.dng','.png','.wav')

class Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]
    def handle_starttag(self, tag, attrs):
        if tag.lower()=='a':
            self.links.extend(v for k,v in attrs if k.lower()=='href' and v)

class Camera:
    def __init__(self, log, host='192.168.42.1', port=80, cache_dir=None):
        self.host,self.port,self.log=host,port,log
        self.block=1024*1024
        self.cache_limit=128  # 128 MiB RAM
        self.disk_limit=2*1024**3
        self.cache=collections.OrderedDict()
        self.disk=collections.OrderedDict()
        self.disk_bytes=0
        self.lock=threading.RLock()
        self.bytes_received=self.requests=self.cache_hits=self.disk_hits=0
        self.read_retries=0
        self.executor=ThreadPoolExecutor(max_workers=2,thread_name_prefix='x5-read')
        self.pending={}; self.last_reads={}; self.stopping=False
        self.pool=queue.LifoQueue()
        for _ in range(2):
            self.pool.put(http.client.HTTPConnection(host,port,timeout=8))
        self.cache_dir=Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True,exist_ok=True)
            # This directory contains only this app's expendable block cache.
            for p in self.cache_dir.glob('x5-*.blk'):
                if p.is_file(): p.unlink()
        self.log('cache_config', memory_mib=128,disk_mib=2048,block_mib=1,connections=2,connection_mode='isolated_requests')

    def path(self,url):
        p=urlsplit(url)
        if p.scheme or p.netloc:
            if p.scheme!='http' or p.hostname!=self.host or p.port not in (None,self.port) or p.username:
                raise ValueError('非相机地址')
        decoded=unquote(p.path)
        if not decoded.startswith('/') or decoded.startswith('//') or '..' in decoded.split('/') or '\x00' in decoded:
            raise ValueError('无效文件地址')
        return quote(decoded,safe='/%:@-._~')+('?' + p.query if p.query else '')

    def request(self,url,payload=None,span=None,raw_limit=None):
        path=self.path(url)
        headers={'X-XSRF-Protected':'1','Accept-Encoding':'identity','Connection':'close'}
        body=None
        if payload is not None:
            body=json.dumps(payload).encode();headers['Content-Type']='application/json;charset=utf-8'
        if span is not None:headers['Range']='bytes=%d-%d'%span
        conn=self.pool.get()
        try:
            # This X5 firmware leaves bytes behind on persistent Range connections.
            # Fresh sockets prevent one clip's body from becoming the next HTTP header.
            attempts=3 if payload is None else 1
            for attempt in range(attempts):
                conn.close()
                started=time.monotonic();reused=conn.sock is not None
                try:
                    conn.request('POST' if body is not None else 'GET',path,body,headers)
                    r=conn.getresponse()
                    if span is not None:
                        if r.status!=206:raise RangeResponseError('HTTP %d，未读取原片正文'%r.status)
                        m=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',r.getheader('Content-Range',''))
                        if not m:raise RangeResponseError('无效 Content-Range')
                        lo,hi,total=map(int,m.groups());count=hi-lo+1
                        if lo!=span[0] or hi!=min(span[1],total-1) or not 0<=lo<=hi<total:
                            raise RangeResponseError('相机返回位置不符')
                        if count>self.block or r.getheader('Content-Encoding','identity')!='identity':
                            raise RangeResponseError('分段大小或编码不符')
                        length=r.getheader('Content-Length')
                        if length is not None and int(length)!=count:raise RangeResponseError('长度不符')
                        data=r.read(count)
                        if len(data)!=count:raise http.client.IncompleteRead(data, count-len(data))
                        # Close this response/socket after the requested bytes. The firmware
                        # may transmit extra body bytes even after Content-Length is exhausted.
                        with self.lock:
                            self.bytes_received+=len(data);self.requests+=1
                        self.log('range',path=path,start=lo,length=count,total=total,
                                 reused=reused,elapsed=round(time.monotonic()-started,4))
                        return data,total
                    if r.status!=200:raise RangeResponseError('HTTP %d'%r.status)
                    limit=raw_limit if raw_limit is not None else 4*1024*1024
                    length=r.getheader('Content-Length')
                    if length and int(length)>limit:raise IOError('目录响应超过限制')
                    raw=r.read(limit+1)
                    if len(raw)>limit:raise IOError('目录响应超过限制')
                    if raw_limit is not None:return raw
                    result=json.loads(raw)
                    if not isinstance(result,dict):raise IOError('OSC 非对象响应')
                    return result
                except (OSError, http.client.HTTPException) as exc:
                    conn.close()
                    reason=read_error_text(exc)
                    if attempt+1>=attempts:
                        raise IOError(reason) from None
                    with self.lock:self.read_retries+=1
                    self.log('read_retry',path=path,attempt=attempt+1,error=reason)
                    time.sleep(.1*(attempt+1))
                except Exception:
                    conn.close();raise
        finally:
            conn.close()
            self.pool.put(conn)

    def command(self,parameters):
        result=self.request('/osc/commands/execute',{'name':'camera.listFiles','parameters':parameters})
        for _ in range(10):
            if result.get('state')!='inProgress':break
            time.sleep(1)
            result=self.request('/osc/commands/status',{'id':result['id']})
        if result.get('state')!='done':raise IOError(json.dumps(result.get('error',result),ensure_ascii=False))
        return result['results']

    def catalog(self):
        info=self.request('/osc/info')
        if info.get('model')!='Insta360 X5':raise IOError('未识别到 X5')
        self.log('camera',model=info.get('model'),firmware=info.get('firmwareVersion'))
        state=self.request('/osc/state',{})
        candidates={};sources=collections.Counter()
        def add(url,source):
            try:path=self.path(url)
            except (ValueError,TypeError):return
            if not path.startswith('/DCIM/') or not urlsplit(path).path.lower().endswith(MEDIA):return
            if path not in candidates:candidates[path]=source;sources[source]+=1
        # Do not trust totalEntries or a short page: some firmware returns only a subset.
        for file_type in ('all','video','image'):
            position=0;seen=set()
            for page in range(200):
                parameters={'fileType':file_type,'entryCount':100,'maxThumbSize':0,'startPosition':position}
                try:result=self.command(parameters)
                except Exception as exc:
                    self.log('catalog_error',source='osc',parameters=parameters,error=str(exc));break
                batch=result.get('entries',[])
                paths=[e.get('fileUrl','') for e in batch if isinstance(e,dict)]
                fresh=set(paths)-seen
                self.log('catalog_page',parameters=parameters,returned=len(batch),
                         total=result.get('totalEntries'),fresh=len(fresh),files=paths)
                for url in paths:add(url,'osc')
                if not batch or not fresh:break
                seen.update(paths);position+=len(batch)
                if page==199:self.log('catalog_warning',message='OSC 达到分页上限')
        # Follow only observed links within DCIM; also inspect the known camera media index.
        root=state.get('state',{}).get('storageUri','/DCIM/Camera01/')
        directories=collections.deque(['/DCIM/',root])
        directories.extend(sorted({p.rsplit('/',1)[0]+'/' for p in candidates}))
        visited=set();index_paths={'/DCIM/fileinfo_list.list'}
        while directories and len(visited)<64:
            try:path=self.path(directories.popleft())
            except ValueError:continue
            if path in visited or not path.startswith('/DCIM/'):continue
            visited.add(path)
            try:
                raw=self.request(path,raw_limit=4*1024*1024)
                parser=Links();parser.feed(raw.decode('utf-8',errors='replace'))
                count=0
                for href in parser.links:
                    linked=urljoin(path,href)
                    try:linked=self.path(linked)
                    except ValueError:continue
                    if not linked.startswith('/DCIM/'):continue
                    if linked.endswith('/') and linked not in visited:directories.append(linked)
                    elif linked.lower().endswith('.list'):index_paths.add(linked)
                    else:add(linked,'directory');count+=1
                self.log('catalog_directory',path=path,bytes=len(raw),links=len(parser.links),media_links=count)
            except Exception as exc:self.log('catalog_directory_error',path=path,error=str(exc))
        for path in sorted(index_paths)[:16]:
            try:
                raw=self.request(path,raw_limit=32*1024*1024)
                # Read index as data only. Never modify it or trust filenames without an HTTP check.
                matches=re.findall(rb'(?:/DCIM/)?Camera[^/\x00\s"<>]{1,32}/[A-Za-z0-9_.-]+\.(?:insv|insp|lrv|mp4|jpg|jpeg|dng|png|wav)',raw.replace(b'\\/',b'/'),re.I)
                for match in matches:
                    name=match.decode('ascii');add(name if name.startswith('/') else '/DCIM/'+name,'index')
                self.log('catalog_index',path=path,bytes=len(raw),candidates=len(matches))
            except Exception as exc:self.log('catalog_index_error',path=path,error=str(exc))
        entries=[];names={}
        for url,source in sorted(candidates.items()):
            name=unquote(urlsplit(url).path.rsplit('/',1)[-1])
            if name in names and names[name]!=url:
                self.log('catalog_name_collision',file=name,path=url)
                raise IOError('不同目录存在同名文件；为避免素材关联错误，暂未挂载')
            try:_,size=self.request(url,span=(0,0))
            except Exception as exc:
                self.log('catalog_file_error',path=url,source=source,error=str(exc));continue
            names[name]=url;entries.append({'name':name,'url':url,'size':size})
        dates=sorted({m.group(0) for e in entries if (m:=re.search(r'20\d{6}',e['name']))})
        mounted={e['name'] for e in entries}
        originals=[n for n in mounted if n.startswith('VID_') and n.endswith('.insv')]
        missing_proxies=[n for n in originals if ('LRV_'+n[4:].replace('_00_','_01_').removesuffix('.insv')+'.lrv') not in mounted]
        failed_files=len(candidates)-len(entries)
        self.log('catalog_complete',files=len(entries),sources=dict(sources),dates=dates,
                 failed_files=failed_files,missing_proxies=missing_proxies,
                 completeness='unverified',files_detail=entries)
        print('  找到 %d 个文件；文件名日期：%s'%(len(entries),'、'.join(dates)),flush=True)
        print('  目录完整性仍需与你的相机相册核对。',flush=True)
        return entries

    def _put(self,key,data):
        with self.lock:
            self.cache[key]=data;self.cache.move_to_end(key)
            while len(self.cache)>self.cache_limit:self.cache.popitem(last=False)
            if self.cache_dir and key not in self.disk:
                while self.disk and self.disk_bytes+len(data)>self.disk_limit:
                    _,(old,size)=self.disk.popitem(last=False)
                    old.unlink(missing_ok=True);self.disk_bytes-=size
                if len(data)<=self.disk_limit:
                    p=self.cache_dir/('x5-'+hashlib.sha256(repr(key).encode()).hexdigest()+'.blk')
                    try:
                        p.write_bytes(data);self.disk[key]=(p,len(data));self.disk_bytes+=len(data)
                    except OSError as exc:
                        p.unlink(missing_ok=True);self.log('cache_write_error',error=str(exc))
    def _cached(self,key):
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key);self.cache_hits+=1;return self.cache[key]
            if key in self.disk:
                p,size=self.disk[key]
                try:data=p.read_bytes()
                except OSError:data=b''
                if len(data)==size:
                    self.disk.move_to_end(key);self.disk_hits+=1
                    self.cache[key]=data
                    while len(self.cache)>self.cache_limit:self.cache.popitem(last=False)
                    return data
                self.disk.pop(key);self.disk_bytes-=size;p.unlink(missing_ok=True)
        return None
    def _fetch(self,node,base):
        data,size=self.request(node['url'],span=(base,min(base+self.block,node['size'])-1))
        if size!=node['size']:raise IOError('素材大小发生改变，请停止拍摄后重新挂载')
        self._put((node['url'],base),data)
        return data
    def _future(self,node,base,prefetch=False):
        key=(node['url'],base)
        with self.lock:
            if key in self.pending:return self.pending[key]
            if self.stopping or (prefetch and len(self.pending)>=4):return None
            future=self.executor.submit(self._fetch,node,base)
            self.pending[key]=future
            def done(f):
                with self.lock:
                    if self.pending.get(key) is f:self.pending.pop(key,None)
                if f.exception():self.log('fetch_error',path=node['url'],offset=base,error=str(f.exception()))
            future.add_done_callback(done)
            return future
    def read(self,node,offset,count):
        end=min(node['size'],offset+count)
        if offset>=end:return b''
        initial=offset;pieces=[]
        while offset<end:
            base=offset//self.block*self.block;key=(node['url'],base)
            data=self._cached(key)
            if data is None:
                future=self._future(node,base)
                if future is None:raise IOError('相机连接正在关闭')
                data=future.result()
            stop=min(end,base+len(data))
            if stop<=offset:raise IOError('空分段响应')
            pieces.append(data[offset-base:stop-base]);offset=stop
        with self.lock:
            previous=self.last_reads.get(node['url']);self.last_reads[node['url']]=end
        # Read ahead only after sequential movement; never prefetch entire clips.
        if previous is not None and 0<=initial-previous<=self.block:
            next_base=(end+self.block-1)//self.block*self.block
            for base in (next_base,next_base+self.block):
                if base<node['size'] and self._cached((node['url'],base)) is None:self._future(node,base,True)
        return b''.join(pieces)
    def benchmark(self,entries):
        videos=[e for e in entries if e['name'].lower().endswith('.insv') and e['size']>16*self.block]
        if not videos:return
        node=videos[0];started=time.monotonic()
        try:
            for group in range(4):
                futures=[self._future(node,(1+group*2+i)*self.block) for i in range(2)]
                for future in futures:future.result()
            elapsed=time.monotonic()-started
            rate=8*self.block/1048576/elapsed
            self.log('benchmark',mib=8*self.block/1048576,seconds=elapsed,mib_per_second=rate)
            print('  小样本读取速度约 %.1f MiB/s（不是播放流畅度保证）。'%rate,flush=True)
        except Exception as exc:self.log('benchmark_error',error=str(exc))

    def close(self):
        self.stopping=True;self.executor.shutdown(wait=True)
        while not self.pool.empty():self.pool.get().close()
        self.log('cache_summary',memory_hits=self.cache_hits,disk_hits=self.disk_hits,
                 disk_bytes=self.disk_bytes,network_bytes=self.bytes_received,requests=self.requests)
        for p,size in self.disk.values():p.unlink(missing_ok=True)
        self.disk.clear();self.cache.clear();self.disk_bytes=0
