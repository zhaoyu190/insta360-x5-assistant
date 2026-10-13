"""Line-delimited JSON lifecycle for the native Mac app."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from bridge import Camera,Log,Server,Handler,Volume
from live_camera import LiveCamera

out=sys.stdout
output_lock=threading.Lock()
commands=queue.Queue()
stop=threading.Event()
def emit(event,**values):
    with output_lock:
        try:out.write(json.dumps(dict(event=event,**values),ensure_ascii=False)+'\n');out.flush()
        except BrokenPipeError:stop.set()
def reader():
    for line in sys.stdin:
        try:
            payload=json.loads(line)
            command=payload.get('command')
            if command in ('disconnect','preview_start','preview_stop','record_start','record_stop','status','mode_set'):
                if command=='disconnect':stop.set()
                commands.put((command,payload.get('mode')))
        except (ValueError,AttributeError):pass
    stop.set();commands.put(('disconnect',None))
def system(command):
    result=subprocess.run(command,capture_output=True,text=True,timeout=30)
    if result.returncode and any(s in result.stderr.lower() for s in ('operation not permitted','permission denied','must be root')):
        import shlex
        emit('progress',message='请在 macOS 系统弹窗中允许挂载相机。')
        script='on run argv\n do shell script (item 1 of argv) with administrator privileges\nend run'
        result=subprocess.run(['/usr/bin/osascript','-e',script,shlex.join(command)],capture_output=True,text=True)
    return result

def run(base):
    base.mkdir(parents=True,exist_ok=True)
    lock=(base/'session.lock').open('a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:emit('error',message='已有相机连接正在运行，请先断开。');return 1
    mount=base/'相机素材';mount.mkdir(exist_ok=True)
    if os.path.ismount(mount) or any(mount.iterdir()):
        emit('error',message='素材目录已被占用，请先断开之前的相机连接。');return 1
    existing=subprocess.run(['/sbin/mount'],capture_output=True,text=True).stdout
    if '127.0.0.1:/X5 on ' in existing:
        emit('error',message='之前的相机盘仍在运行，请先在旧窗口断开，再连接。');return 1
    log=Log(base/'运行日志')
    summary={};server=None;camera=None;live=None;last_read_notice=0
    def report(event,**values):
        nonlocal last_read_notice
        log(event,**values)
        if event=='catalog_complete':summary.update(values)
        if event=='benchmark':summary['speed']=values.get('mib_per_second',0)
        if event=='catalog_page':emit('progress',message='正在查找相机中的素材…')
        elif event=='catalog_index':emit('progress',message='正在核对相机素材索引…')
        elif event=='read_error' and time.monotonic()-last_read_notice>10:
            last_read_notice=time.monotonic()
            emit('read_issue',message='素材读取失败：'+values.get('error','')+'。请保持 X5 Wi-Fi 连接，再重试导出。')
    threading.Thread(target=reader,daemon=True).start()
    try:
        emit('progress',message='正在连接 X5…',log=str(log.path))
        live=LiveCamera(emit,report)
        try:live.connect_session()
        except Exception as exc:
            report('live_init_error',error=str(exc))
            emit('connection_health',state='offline',message='控制保活尚未建立，连接后会重试。')
        camera=Camera(report,cache_dir=base/'缓存')
        with contextlib.redirect_stdout(sys.stderr):
            entries=camera.catalog()
            if stop.is_set():return 0
            emit('progress',message='正在准备预览缓存…')
            camera.benchmark(entries)
        if stop.is_set():return 0
        server=Server(('127.0.0.1',0),Handler);server.volume=Volume(entries,camera,report)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        port=server.server_address[1]
        emit('progress',message='正在打开相机素材盘…')
        options=f'vers=3,tcp,port={port},mountport={port},ro,soft,locallocks,noacl,nobrowse,rsize=1048576,readahead=4,timeo=100,retrans=3,retrycnt=0'
        result=system(['/sbin/mount_nfs','-o',options,'127.0.0.1:/X5',str(mount)])
        log('mount',code=result.returncode,error=result.stderr)
        if result.returncode or not os.path.ismount(mount):raise RuntimeError('素材盘连接失败：'+result.stderr.strip())
        emit('connected',path=str(mount),files=len(entries),dates=summary.get('dates',[]),speed=summary.get('speed',0))
        missed=summary.get('failed_files',0)
        proxies=len(summary.get('missing_proxies',[]))
        if missed or proxies:
            emit('read_issue',message=f'素材盘已连接，但有 {missed} 个文件读取失败、{proxies} 段原片未找到配套 LRV，预览或导出可能受影响。请重连后再导入。')
        try:live.sync_status()
        except Exception as exc:
            report('live_init_error',error=str(exc))
            emit('control_error',message='暂时无法读取拍摄状态，可点击“同步状态”重试。')
        try:live.sync_modes()
        except Exception as exc:report('mode_init_error',error=str(exc))
        while True:
            if stop.is_set():
                result=system(['/sbin/umount',str(mount)])
                if not os.path.ismount(mount):break
                emit('busy',message='素材仍在使用。请关闭 Studio 中的素材和预览窗口，再点“断开相机”。')
                stop.clear()
                while not commands.empty():commands.get_nowait()
            try:command,mode=commands.get(timeout=.5)
            except queue.Empty:command=None;mode=None
            if command=='disconnect':stop.set()
            elif command:
                try:
                    if command=='mode_set':live.set_mode(mode)
                    else:
                        {'preview_start':live.start_preview,'preview_stop':live.stop_preview,
                         'record_start':live.start_recording,'record_stop':live.stop_recording,
                         'status':live.refresh_status}[command]()
                except Exception as exc:
                    report('control_error',command=command,error=str(exc))
                    if command.startswith('preview_'):
                        emit('preview_state',state='error',message='预览失败：'+str(exc))
                    else:emit('control_error',message='操作未确认：'+str(exc))
                finally:emit('control_done',command=command)
            if not stop.is_set():live.tick()
            emit('stats',received=camera.bytes_received,memory_hits=camera.cache_hits,disk_hits=camera.disk_hits,
                 cache_bytes=camera.disk_bytes)
        log('unmounted')
    except Exception as exc:
        log('app_error',error=str(exc))
        text=str(exc)
        if isinstance(exc,(TimeoutError,ConnectionError,OSError)) and not os.path.ismount(mount):
            text='无法连接 X5。请确认相机已开启 Wi-Fi，且 Mac 已手动连接相机热点，再重试。'
        emit('error',message=text)
    finally:
        if live:
            try:live.stop_preview()
            except Exception:pass
        # Never stop serving an active mount; keep UI alive if Studio still holds files.
        while os.path.ismount(mount):
            emit('busy',message='素材盘仍在使用，请关闭素材后再次断开。')
            commands.get()
            system(['/sbin/umount',str(mount)])
        if live:live.close()
        if server:server.shutdown();server.server_close()
        if camera:camera.close()
        emit('disconnected')
    return 0

if __name__=='__main__':
    if len(sys.argv)==2 and sys.argv[1]=='--self-test':
        import av
        from PIL import Image
        from fractions import Fraction
        from live_camera import PreviewDecoder
        codec=av.CodecContext.create('libx264','w')
        codec.width=64;codec.height=32;codec.pix_fmt='yuv420p';codec.time_base=Fraction(1,30)
        codec.options={'preset':'ultrafast','tune':'zerolatency'}
        decoded=[]
        decoder=PreviewDecoder(lambda event,**kw:decoded.append(event))
        for i in range(4):
            frame=av.VideoFrame.from_image(Image.new('RGB',(64,32),(30,140,180)));frame.pts=i
            for packet in codec.encode(frame):decoder.feed(bytes(packet))
        for packet in codec.encode(None):decoder.feed(bytes(packet))
        if 'preview_frame' not in decoded:raise RuntimeError('decode self-test failed')
        print(json.dumps({'self_test':'passed','decoded_frames':len(decoded)}))
        raise SystemExit(0)
    if len(sys.argv)!=2:raise SystemExit('data directory required')
    raise SystemExit(run(Path(sys.argv[1])))
