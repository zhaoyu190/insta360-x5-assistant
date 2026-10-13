"""X5 live-view transport and recording controls. GPL-3.0-or-later.
Protocol reference: https://gitlab.com/whitebox-aero/insta360
No recording command is retried automatically. Video frames never touch disk.
"""
import base64
import io
import queue
import socket
import struct
import threading
import time
import av

MODES = {
    'panorama': ('360° 全景录像', 3, 0),
    'front': ('单镜头 · 前镜头', 1, 0),
    'rear': ('单镜头 · 后镜头', 2, 0),
    # X5 firmware A3_1.13.21.623_build1, observed after selecting InstaFrame
    # on the camera. Do not infer this value from older SDK enum tables.
    'instaframe': ('云台 / 动态跟拍', 3, 15),
}

def varint(value):
    if not isinstance(value, int) or not 0 <= value <= 0xffffffff:
        raise ValueError('无效协议参数')
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128); value >>= 7
    return bytes(out) + bytes([value])


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
                if now-self.last < 1/15: continue
                self.last = now; self.frames += 1
                image = frame.to_image()
                image.thumbnail((1440, 1440))
                output = io.BytesIO(); image.save(output, format='JPEG', quality=82)
                self.emit('preview_frame', jpeg=base64.b64encode(output.getvalue()).decode('ascii'),
                          width=image.width, height=image.height)


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
        self.sensor = None
        self.video_mode = None
        self.mode_dirty = True
        self.session_active = False
        self.last_retry = 0
        self.last_received = time.monotonic()
        self.command_timeout = 6
        self.capture_timeout = 10

    def connect_session(self):
        """Keep a phone-style control session for the entire material mount."""
        self.session_active = True
        self.last_retry = time.monotonic()
        self.sync_status()
        self.emit("connection_health", state="online", message="控制连接在线 · 每 2 秒保活")

    def open(self):
        if self.socket and not self.halt.is_set(): return
        self._close_transport()
        self.halt = threading.Event()
        self.socket = socket.create_connection((self.host, self.tcp_port), timeout=4)
        self.socket.settimeout(1)
        self.last_received = time.monotonic()
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
            try: result = response.get(timeout=self.command_timeout)
            except queue.Empty:
                # Preview/heartbeat traffic can remain alive while commands stop
                # responding. Invalidate this session even if bytes still arrive.
                self.log('control_timeout', code=code, sequence=sequence)
                self._close_transport()
                self.recording = 'unknown'; self.emit('record_state', state='unknown')
                self.emit('preview_state', state='error', message='控制响应超时，正在恢复连接；可重新开启预览。')
                self.emit('connection_health', state='offline', message='控制响应超时，正在恢复连接…')
                raise TimeoutError('相机控制响应超时；将重新建立控制连接，不会重复发送拍摄指令')
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
                self.last_received = time.monotonic()
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
                self.emit('connection_health', state='offline', message='控制连接已断开，正在尝试恢复…')
                self.emit('camera_mode', mode='', sensor=0, video_mode=-1, label='拍摄模式未确认', modes=[])
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
                try: self.video.put_nowait((self.decoder, packet[12:]))
                except queue.Full:
                    # Never grow a delayed preview; fail visibly and allow a fresh keyframe stream.
                    self.preview_failed = True
            return
        if packet[:3] != b'\x04\x00\x00': return
        code = int.from_bytes(packet[3:5], 'little')
        seq = int.from_bytes(packet[6:9], 'little')
        if code == 8275:  # CAMERA_NOTIFICATION_CAM_SUBMODE_CHANGE
            self.mode_dirty = True
            return
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
        # X5 InstaFrame reports 30 (observed with elapsed capture time and
        # a saved video), absent from the older public protocol enum.
        elif value in (1,2,7,9,12,13,17,18,20,21,22,23,24,30): state = 'recording'
        elif value in (3,4,5,6,8,10,11,14,15,16,19): state = 'busy'
        else: state = 'unknown'  # Keep Stop available for newer firmware states.
        self.log('capture_status', value=value, substate=status.get(10, 0), seconds=status.get(2, 0))
        self.recording = state; self.last_status = time.monotonic()
        self.emit('record_state', state=state)

    def sync_status(self):
        result = fields(self.command(15))
        if not isinstance(result.get(1), bytes): raise ValueError('相机未返回可识别的录像状态')
        self.status_payload(result[1])
        self.emit('connection_health', state='online', message='控制连接在线 · 每 2 秒保活')

    def sync_modes(self):
        sensor = fields(self.command(66)).get(1)
        response = fields(self.command(8, b'\x0a\x02\x28\x29'))
        values = fields(response.get(2, b''))
        video = values.get(41)
        if sensor not in (1, 2, 3) or not isinstance(video, int):
            self.sensor = self.video_mode = None
            raise RuntimeError('此固件未返回可识别的拍摄模式')
        self.sensor, self.video_mode = sensor, video
        current = next((key for key, (_, s, v) in MODES.items() if (s, v) == (sensor, video)), '')
        self.mode_dirty = False
        self.emit('camera_mode', mode=current, sensor=sensor, video_mode=video,
                  label=MODES[current][0] if current else '相机当前模式（请在相机上调整）',
                  modes=[{'id':key,'label':value[0]} for key,value in MODES.items()])
        return sensor, video

    def refresh_status(self):
        self.sync_status()
        self.sync_modes()

    def set_mode(self, mode):
        if mode not in MODES: raise ValueError('不支持的拍摄模式')
        self.sync_status()
        if self.recording != 'idle': raise RuntimeError('请先停止拍摄并等待保存完成，再切换模式。')
        resume = self.preview
        if resume: self.stop_preview()
        try:
            _, sensor, video = MODES[mode]
            self.command(65, b'\x08' + varint(sensor))
            # SetOptions.option_types=[VIDEO_SUB_MODE], value.video_sub_mode.
            value = varint(41 << 3) + varint(video)
            self.command(7, b'\x0a\x01\x29\x12' + varint(len(value)) + value)
            actual = self.sync_modes()
            if actual != (sensor, video):
                raise RuntimeError('相机未采用所选模式，已显示相机实际状态。请检查固件或相机屏幕。')
            self.emit('control_message', message='已切换至'+MODES[mode][0]+'。仅切换模式，尚未开始拍摄。')
        except Exception:
            # A rejected second command can leave a partial change: read back,
            # never show the requested mode as if the transaction succeeded.
            try: self.sync_modes()
            except Exception:
                self.sensor = self.video_mode = None
                self.emit('camera_mode', mode='', sensor=0, video_mode=-1, label='拍摄模式未确认', modes=[])
            raise
        finally:
            if resume:
                try: self.start_preview()
                except Exception as exc:
                    self.emit('preview_state', state='error', message='模式已处理，但预览未恢复：'+str(exc))

    def keepalive(self):
        while not self.halt.wait(2):
            try:
                if time.monotonic()-self.last_received > 15:
                    self.socket.shutdown(socket.SHUT_RDWR)
                    return
                self.send(b'\x05\x00\x00')
            except OSError:
                try: self.socket.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                return

    def decode(self):
        while not self.halt.is_set():
            try: decoder, data = self.video.get(timeout=.5)
            except queue.Empty: continue
            if not self.preview or decoder is None or decoder is not self.decoder: continue
            try:
                before = decoder.frames
                decoder.feed(data)
                if decoder is self.decoder and decoder.frames > before: self.last_frame = time.monotonic()
            except Exception as exc:
                self.log('preview_decode_error', error=str(exc))
                if decoder is self.decoder: self.preview_failed = True

    def start_preview(self):
        self.open()
        if self.preview: return
        while not self.video.empty(): self.video.get_nowait()
        def emit_current(event, **values):
            if self.preview and self.decoder is decoder: self.emit(event, **values)
        decoder = PreviewDecoder(emit_current)
        self.decoder = decoder
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
            self._close_transport()
            raise

    def stop_preview(self):
        active = self.preview
        self.preview = False
        if active and not self.halt.is_set(): self.command(2)
        self.decoder = None
        self.emit('preview_state', state='off', message='预览已关闭；不会影响相机录像。')

    def confirm_capture(self, expected):
        """An ACK only accepts a request. Require actual capture status as proof."""
        deadline = time.monotonic() + self.capture_timeout
        while True:
            self.sync_status()
            if self.recording == expected: return
            if time.monotonic() >= deadline:
                raise RuntimeError('相机未确认'+('开始录像' if expected == 'recording' else '停止录像')+'；已显示实际状态，请检查相机屏幕。')
            time.sleep(.25)

    def capture_command(self, code, body, expected):
        try:
            result = self.command(code, body)
        except Exception:
            # Lost ACKs must never cause a second start/stop. Reconcile by
            # reading only, so an uncertain result does not lock the UI forever.
            try: self.sync_status()
            except Exception:
                self.recording = 'unknown'; self.emit('record_state', state='unknown')
            raise
        self.confirm_capture(expected)
        return result

    def start_recording(self):
        self.sync_status()
        if self.recording != 'idle': raise RuntimeError('相机正在拍摄或处理素材，请先停止或等待完成。')
        self.sync_modes()
        if self.video_mode == 100: raise RuntimeError('当前是拍照模式，请先选择一种录像模式。')
        # Preserve the selected lens and submode; setting captureMode=video here
        # can silently reset InstaFrame or another mode to normal video.
        # X5 OSC can report "done" without changing capture state. Use the
        # same TCP control session as preview, with Capture_MODE_NORMAL=1.
        self.capture_command(4, b'\x08\x01', 'recording')
        self.emit('control_message', message='已开始录像，视频保存在相机存储卡。')

    def stop_recording(self):
        result = self.capture_command(5, b'', 'idle')
        self.emit('control_message', message='录像已停止并保存在相机。重新连接后可在素材盘看到新视频。')
        return result

    def tick(self):
        if self.session_active and (not self.socket or self.halt.is_set()):
            if time.monotonic()-self.last_retry < 8: return
            self.last_retry = time.monotonic()
            try:
                self.connect_session()
                self.sync_modes()
            except Exception as exc:
                self.log('control_reconnect_error', error=str(exc))
                self.emit('connection_health', state='offline', message='等待 X5 Wi-Fi 恢复；关机后需先手动开机。')
            return
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
        if self.mode_dirty and self.socket and not self.halt.is_set():
            try: self.sync_modes()
            except Exception as exc:
                self.mode_dirty = False
                self.log('mode_status_error', error=str(exc))

    def close(self):
        self.session_active = False
        self._close_transport()

    def _close_transport(self):
        self.halt.set(); self.preview=False
        if self.socket:
            try: self.socket.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            self.socket.close()
        for thread in self.threads:
            if thread is not threading.current_thread(): thread.join(timeout=2)
        self.socket=None; self.decoder=None
