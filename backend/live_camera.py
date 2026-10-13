"""X5 live-view transport and recording controls. GPL-3.0-or-later.
Protocol reference: https://gitlab.com/whitebox-aero/insta360
No recording command is retried automatically. Video frames never touch disk.
"""
import base64
import http.client
import io
import json
import queue
import socket
import struct
import threading
import time
import av


def fields(data):
    """Read bounded protobuf wire fields without requiring generated schemas."""
    pos = 0
    def varint():
        nonlocal pos
        value = 0
        for shift in range(0, 70, 7):
            if pos >= len(data): raise ValueError('截断的状态数据')
            byte = data[pos]; pos += 1; value |= (byte & 127) << shift
            if byte < 128: return value
        raise ValueError('无效状态数据')
    result = {}
    while pos < len(data):
        tag = varint(); wire = tag & 7
        if wire == 0: value = varint()
        elif wire in (1, 2, 5):
            size = varint() if wire == 2 else (8 if wire == 1 else 4)
            if size > len(data)-pos: raise ValueError('截断的状态字段')
            value = data[pos:pos+size]; pos += size
        else: raise ValueError('不支持的状态字段')
        result[tag >> 3] = value
    return result


class PreviewDecoder:
    def __init__(self, emit):
        self.emit = emit
        self.codec = av.CodecContext.create('h264', 'r')
        self.codec.thread_count = 1
        self.last = 0
        self.frames = 0
    def feed(self, data):
        for packet in self.codec.parse(data):
            for frame in self.codec.decode(packet):
                now = time.monotonic()
                if now-self.last < 0.10: continue
                self.last = now; self.frames += 1
                image = frame.to_image()
                image.thumbnail((960, 540))
                output = io.BytesIO(); image.save(output, format='JPEG', quality=75)
                self.emit('preview_frame', jpeg=base64.b64encode(output.getvalue()).decode('ascii'))


class LiveCamera:
    def __init__(self, emit, log, host='192.168.42.1', tcp_port=6666, http_port=80):
        self.emit, self.log = emit, log
        self.host, self.tcp_port, self.http_port = host, tcp_port, http_port
        self.socket = None
        self.halt = threading.Event()
        self.send_lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self.pending = {}
        self.sequence = 0
        self.preview = False
        self.decoder = None
        self.video = queue.Queue(maxsize=24)
        self.recording = 'unknown'
        self.last_frame = 0
        self.last_status = 0
        self.preview_failed = False
        self.threads = []
        self.stream_types = set()

    def open(self):
        if self.socket and not self.halt.is_set(): return
        self.close()
        self.halt = threading.Event()
        self.socket = socket.create_connection((self.host, self.tcp_port), timeout=4)
        self.socket.settimeout(1)
        self.send(b'\x06\x00\x00syNceNdinS')
        self.send(b'\x05\x00\x00')
        self.threads = [threading.Thread(target=f, daemon=True) for f in (self.receive, self.keepalive, self.decode)]
        for thread in self.threads: thread.start()

    def send(self, body):
        with self.send_lock:
            if not self.socket or self.halt.is_set(): raise ConnectionError('相机控制连接已断开')
            self.socket.sendall(struct.pack('<I', len(body)+4)+body)

    def command(self, code, body=b''):
        self.open()
        self.sequence = (self.sequence % 0xffffff)+1
        sequence = self.sequence
        response = queue.Queue(maxsize=1)
        with self.pending_lock: self.pending[sequence] = response
        try:
            self.send(b'\x04\x00\x00'+struct.pack('<H', code)+b'\x02'+sequence.to_bytes(3,'little')+b'\x80\x00\x00'+body)
            self.log('live_command', code=code, sequence=sequence)
            try: result = response.get(timeout=6)
            except queue.Empty: raise TimeoutError('相机控制响应超时，请检查 Wi-Fi 和相机屏幕')
            if isinstance(result, Exception): raise result
            status, payload = result
            self.log('live_response', code=code, status=status, payload_bytes=len(payload))
            if status != 200:
                message = fields(payload).get(2, '相机拒绝操作'.encode())
                raise RuntimeError(message.decode('utf-8','replace') if isinstance(message, bytes) else str(message))
            return payload
        finally:
            with self.pending_lock: self.pending.pop(sequence, None)

    def receive(self):
        buffer = bytearray()
        try:
            while not self.halt.is_set():
                try: chunk = self.socket.recv(65536)
                except socket.timeout: continue
                if not chunk: raise ConnectionError('相机控制连接已断开')
                buffer.extend(chunk)
                while len(buffer) >= 4:
                    size = struct.unpack_from('<I', buffer)[0]
                    if not 7 <= size <= 8*1024*1024: raise ValueError('无法识别相机预览协议')
                    if len(buffer) < size: break
                    packet = bytes(buffer[4:size]); del buffer[:size]
                    self.packet(packet)
        except Exception as exc:
            if not self.halt.is_set():
                self.log('live_connection_error', error=str(exc))
                self.emit('control_error', message=str(exc)+'。录像可能仍在继续，请检查相机。')
                self.recording = 'unknown'; self.emit('record_state', state='unknown')
                self.preview = False; self.emit('preview_state', state='error', message='实时连接已中断，可重新开启预览。')
            self.halt.set()
            with self.pending_lock:
                for target in self.pending.values():
                    try: target.put_nowait(ConnectionError('相机连接中断'))
                    except queue.Full: pass

    def packet(self, packet):
        if len(packet) < 12: return
        if packet[:3] == b'\x01\x00\x00':
            if packet[3] not in self.stream_types:
                self.stream_types.add(packet[3])
                self.log('live_stream_type', stream_type=packet[3], packet_bytes=len(packet))
            if packet[3] == 0x20 and self.preview:
                if len(packet) > 1024*1024:
                    self.preview_failed = True; return
                try: self.video.put_nowait(packet[12:])
                except queue.Full:
                    # Never grow a delayed preview; fail visibly and allow a fresh keyframe stream.
                    self.preview_failed = True
            return
        if packet[:3] != b'\x04\x00\x00': return
        code = int.from_bytes(packet[3:5], 'little')
        seq = int.from_bytes(packet[6:9], 'little')
        if code == 8208:
            self.status_payload(packet[12:]); return
        with self.pending_lock:
            target = self.pending.get(seq)
            if target:
                try: target.put_nowait((code, packet[12:]))
                except queue.Full: pass

    def status_payload(self, data):
        status = fields(data)
        value = status.get(1, 0)
        if value == 0: state = 'idle'
        elif value in (1,2,7,9,12,13,17,18,20,21,22,23,24): state = 'recording'
        else: state = 'busy'
        self.recording = state; self.last_status = time.monotonic()
        self.emit('record_state', state=state)

    def sync_status(self):
        result = fields(self.command(15))
        if not isinstance(result.get(1), bytes): raise ValueError('相机未返回可识别的录像状态')
        self.status_payload(result[1])

    def keepalive(self):
        while not self.halt.wait(2):
            try: self.send(b'\x05\x00\x00')
            except OSError: return

    def decode(self):
        while not self.halt.is_set():
            try: data = self.video.get(timeout=.5)
            except queue.Empty: continue
            decoder = self.decoder
            if not self.preview or decoder is None: continue
            try:
                before = decoder.frames
                decoder.feed(data)
                if decoder.frames > before: self.last_frame = time.monotonic()
            except Exception as exc:
                self.log('preview_decode_error', error=str(exc))
                self.preview_failed = True

    def start_preview(self):
        self.open()
        if self.preview: return
        while not self.video.empty(): self.video.get_nowait()
        self.decoder = PreviewDecoder(self.emit)
        self.preview_failed = False; self.last_frame = time.monotonic()
        self.preview = True
        self.emit('preview_state', state='starting', message='正在等待相机实时画面…')
        try:
            # enableVideo=1, audioSampleRate=48000, resolution=1440x720p30,
            # enableGyro=1, secondary resolution=424x240p15. No audio output.
            self.command(1, bytes.fromhex('10012080f702380940015022'))
            self.log('preview_started')
        except Exception:
            self.preview = False
            self.close()
            raise

    def stop_preview(self):
        active = self.preview
        self.preview = False
        if active and not self.halt.is_set(): self.command(2)
        self.decoder = None
        self.emit('preview_state', state='off', message='预览已关闭；不会影响相机录像。')

    def osc(self, name, parameters=None):
        def request(path, payload):
            conn = http.client.HTTPConnection(self.host, self.http_port, timeout=8)
            try:
                conn.request('POST', path, json.dumps(payload), {'Content-Type':'application/json', 'X-XSRF-Protected':'1'})
                response = conn.getresponse(); raw = response.read(1024*1024+1)
                if response.status != 200 or len(raw)>1024*1024: raise RuntimeError('相机控制 HTTP 响应异常')
                return json.loads(raw)
            finally: conn.close()
        payload = {'name':name}
        if parameters is not None: payload['parameters'] = parameters
        result = request('/osc/commands/execute', payload)
        deadline = time.monotonic()+20
        while result.get('state') == 'inProgress' and result.get('id') and time.monotonic()<deadline:
            time.sleep(.4)
            result = request('/osc/commands/status', {'id':result['id']})
        self.log('capture_command', name=name, response=result)
        if result.get('state') != 'done':
            raise RuntimeError(str(result.get('error', '相机尚未确认操作完成，请检查相机屏幕')))
        return result

    def start_recording(self):
        self.sync_status()
        if self.recording != 'idle': raise RuntimeError('相机正在拍摄或处理素材，请先停止或等待完成。')
        self.osc('camera.setOptions', {'options':{'captureMode':'video'}})
        try:
            self.osc('camera.startCapture')
        except Exception:
            self.recording='unknown'; self.emit('record_state', state='unknown'); raise
        self.recording='recording'; self.emit('record_state', state='recording')
        self.emit('control_message', message='已开始录像，视频保存在相机存储卡。')

    def stop_recording(self):
        try: result = self.osc('camera.stopCapture')
        except Exception:
            self.recording='unknown'; self.emit('record_state', state='unknown'); raise
        self.recording='idle'; self.emit('record_state', state='idle')
        self.emit('control_message', message='录像已停止并保存在相机。重新连接后可在素材盘看到新视频。')
        return result

    def tick(self):
        if self.preview and (self.preview_failed or time.monotonic()-self.last_frame>10):
            try: self.stop_preview()
            except Exception: pass
            self.emit('preview_state', state='error', message='未收到可解码画面。请断开手机连接后重试；当前 X5 固件的预览兼容性可能需要适配。')
        if self.socket and not self.halt.is_set() and time.monotonic()-self.last_status>4:
            try: self.sync_status()
            except Exception as exc:
                self.last_status=time.monotonic()
                self.recording='unknown'; self.emit('record_state',state='unknown')
                self.log('capture_status_error',error=str(exc))

    def close(self):
        self.halt.set(); self.preview=False
        if self.socket:
            try: self.socket.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            self.socket.close()
        for thread in self.threads:
            if thread is not threading.current_thread(): thread.join(timeout=2)
        self.socket=None; self.decoder=None
