import AppKit
import SwiftUI

final class CameraModel: ObservableObject {
    @Published var phase = "idle"
    @Published var message = "完成上方两步后，即可连接相机。"
    @Published var error = false
    @Published var fileCount = 0
    @Published var dates = ""
    @Published var speed: Double = 0
    @Published var received: Double = 0
    @Published var cached: Double = 0
    @Published var previewImage: NSImage?
    @Published var previewState = "off"
    @Published var previewMessage = "点击“开启预览”，查看相机实时画面。"
    @Published var recordState = "unknown"
    @Published var controlMessage = "正在读取相机拍摄状态…"
    @Published var controlBusy = false
    @Published var previewBusy = false
    @Published var cameraMode = ""
    @Published var cameraModeLabel = "正在读取拍摄模式…"
    @Published var cameraModes: [[String: String]] = []
    @Published var sensor = 0
    @Published var connectionHealth = "正在建立控制连接…"
    var process: Process?
    var input: Pipe?
    var output: Pipe?
    var diagnostics: Pipe?
    var mountPath = ""
    var quitting = false
    let parseQueue = DispatchQueue(label: "yingShi.stdout")
    var buffer = Data()
    let frameLock = NSLock()
    var latestFrame: [String: Any]?
    var frameScheduled = false
    let dataURL = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("影石助手", isDirectory: true)
    var running: Bool { process != nil }

    func connect() {
        guard process == nil else { return }
        guard let resources = Bundle.main.resourceURL else { return }
        let executable = resources.appendingPathComponent("Backend/CameraBridge")
        guard FileManager.default.isExecutableFile(atPath: executable.path) else {
            error = true; message = "应用组件缺失，请重新打开完整的影石助手应用。"; return
        }
        error = false; phase = "connecting"; message = "正在连接 X5…"
        fileCount = 0; dates = ""; received = 0; cached = 0; speed = 0
        previewImage = nil; previewState = "off"; recordState = "unknown"
        cameraMode = ""; cameraModes = []; sensor = 0
        connectionHealth = "正在建立控制连接…"
        cameraModeLabel = "正在读取拍摄模式…"
        controlBusy = false; previewBusy = false
        controlMessage = "正在读取相机拍摄状态…"
        let task = Process(); let stdinPipe = Pipe(); let stdoutPipe = Pipe(); let stderrPipe = Pipe()
        input = stdinPipe; output = stdoutPipe; diagnostics = stderrPipe
        task.executableURL = executable; task.arguments = [dataURL.path]
        task.standardInput = stdinPipe; task.standardOutput = stdoutPipe; task.standardError = stderrPipe
        process = task
        buffer = Data()
        stdoutPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { handle.readabilityHandler = nil; return }
            self?.parseQueue.async { [weak self] in self?.consume(data) }
        }
        // Drain backend diagnostics so progress output cannot block the bridge.
        stderrPipe.fileHandleForReading.readabilityHandler = { handle in
            if handle.availableData.isEmpty { handle.readabilityHandler = nil }
        }
        task.terminationHandler = { [weak self] task in
            DispatchQueue.main.asyncAfter(deadline: .now()+0.1) {
                guard let self = self else { return }
                self.output?.fileHandleForReading.readabilityHandler = nil
                self.diagnostics?.fileHandleForReading.readabilityHandler = nil
                self.input = nil; self.output = nil; self.diagnostics = nil; self.process = nil
                if self.phase != "idle" && !self.error {
                    self.error = true; self.message = "连接服务已结束，请重新连接。"
                }
                self.phase = "idle"
                self.previewImage = nil; self.previewState = "off"
                self.recordState = "unknown"; self.controlBusy = false; self.previewBusy = false
                if self.quitting { NSApp.terminate(nil) }
            }
        }
        do { try task.run() }
        catch {
            process = nil; phase = "idle"; self.error = true
            message = "无法启动连接服务：\(error.localizedDescription)"
        }
    }
    func consume(_ data: Data) {
        buffer.append(data)
        while let end = buffer.firstIndex(of: 10) {
            let line = Data(buffer[..<end]); buffer.removeSubrange(...end)
            guard let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else { continue }
            if obj["event"] as? String == "preview_frame" {
                frameLock.lock(); latestFrame = obj
                let schedule = !frameScheduled; frameScheduled = true; frameLock.unlock()
                if schedule {
                    DispatchQueue.main.async { [weak self] in
                        guard let self = self else { return }
                        self.frameLock.lock(); let frame = self.latestFrame
                        self.latestFrame = nil; self.frameScheduled = false; self.frameLock.unlock()
                        if let frame = frame { self.event(frame) }
                    }
                }
                continue
            }
            DispatchQueue.main.async { [weak self] in self?.event(obj) }
        }
    }
    func event(_ obj: [String: Any]) {
        switch obj["event"] as? String {
        case "preview_frame":
            guard phase == "connected", previewState == "starting" || previewState == "on",
                  let value = obj["jpeg"] as? String, let data = Data(base64Encoded: value),
                  let image = NSImage(data: data) else { return }
            previewImage = image; previewState = "on"
        case "preview_state":
            previewState = obj["state"] as? String ?? "off"
            previewMessage = obj["message"] as? String ?? ""
            if previewState != "on" { previewImage = nil }
        case "record_state":
            recordState = obj["state"] as? String ?? "unknown"
            if controlMessage == "正在读取相机拍摄状态…" { controlMessage = "录像保存在相机存储卡；预览只用于取景。" }
        case "connection_health":
            connectionHealth = obj["message"] as? String ?? "控制连接未确认"
        case "camera_mode":
            cameraMode = obj["mode"] as? String ?? ""
            cameraModeLabel = obj["label"] as? String ?? "模式未确认"
            cameraModes = obj["modes"] as? [[String: String]] ?? []
            sensor = obj["sensor"] as? Int ?? 0
        case "control_message", "control_error":
            controlMessage = obj["message"] as? String ?? ""
        case "read_issue":
            error = true
            message = obj["message"] as? String ?? "素材读取异常，请检查相机连接。"
        case "control_done":
            if (obj["command"] as? String ?? "").hasPrefix("preview_") { previewBusy = false }
            else { controlBusy = false }
        case "progress":
            if phase != "disconnecting" { message = obj["message"] as? String ?? "正在准备…" }
        case "connected":
            phase = "connected"; error = false; message = "相机已连接，可以在访达或 Studio 中打开素材。"
            mountPath = obj["path"] as? String ?? ""
            fileCount = obj["files"] as? Int ?? 0
            let values = obj["dates"] as? [String] ?? []
            dates = "\(values.count) 个拍摄日期"
            speed = obj["speed"] as? Double ?? 0
            if quitting { disconnect() }
        case "stats":
            received = (obj["received"] as? Double ?? 0)/1048576
            cached = (obj["cache_bytes"] as? Double ?? 0)/1048576
        case "busy":
            phase = "connected"; error = true; quitting = false
            message = obj["message"] as? String ?? "素材仍在使用，请关闭后重试。"
        case "error":
            error = true; quitting = false
            message = obj["message"] as? String ?? "连接失败，请重试。"
        case "disconnected":
            phase = "idle"
            previewImage = nil; previewState = "off"
            if !error { message = "相机已断开，临时缓存已清理。" }
        default: break
        }
    }
    func disconnect() {
        if recordState == "recording" || controlBusy {
            controlMessage = "请先停止录像并等待相机确认，再断开连接。"; quitting = false; return
        }
        guard let input = input else { return }
        phase = "disconnecting"; message = "正在安全断开相机…"; error = false
        try? input.fileHandleForWriting.write(contentsOf: Data("{\"command\":\"disconnect\"}\n".utf8))
    }
    func control(_ command: String, mode: String? = nil) {
        guard phase == "connected", let input = input else { return }
        if command.hasPrefix("preview_") {
            previewBusy = true
            previewState = command == "preview_start" ? "starting" : "off"
            previewImage = nil
            previewMessage = command == "preview_start" ? "正在等待相机实时画面…" : "正在关闭预览…"
        } else {
            controlBusy = true
            controlMessage = command == "mode_set" ? "正在切换并核对相机模式…" : command == "record_start" ? "正在请求开始录像…" : command == "record_stop" ? "正在停止并保存录像…" : "正在读取相机拍摄状态…"
        }
        do {
            var payload = ["command": command]
            if let mode = mode { payload["mode"] = mode }
            var data = try JSONSerialization.data(withJSONObject: payload); data.append(10)
            try input.fileHandleForWriting.write(contentsOf: data)
        }
        catch { controlBusy = false; previewBusy = false; controlMessage = "无法发送操作，请重新连接相机。" }
    }
    func openFiles() {
        guard !mountPath.isEmpty else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: mountPath))
    }
    func openStudio() {
        let app = URL(fileURLWithPath: "/Applications/Insta360 Studio.app")
        guard FileManager.default.fileExists(atPath: app.path) else {
            error = true; message = "未找到 Insta360 Studio，请先安装影石官方桌面软件。"; return
        }
        NSWorkspace.shared.openApplication(at: app, configuration: NSWorkspace.OpenConfiguration())
    }
    func openWiFi() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.wifi-settings-extension") {
            NSWorkspace.shared.open(url)
        }
    }
    func openLogs() {
        let url = dataURL.appendingPathComponent("运行日志")
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        NSWorkspace.shared.open(url)
    }
}

struct SetupStep: View {
    let number: String
    let title: String
    let detail: String
    let symbol: String
    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            Text(number).font(.system(size: 13, weight: .semibold, design: .rounded))
                .foregroundStyle(.secondary).frame(width: 27, height: 27)
                .background(Color.primary.opacity(0.055), in: Circle())
            VStack(alignment: .leading, spacing: 6) {
                Text(title).font(.system(size: 15, weight: .semibold))
                Text(detail).font(.system(size: 13)).foregroundStyle(.secondary).lineSpacing(3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            Image(systemName: symbol).font(.system(size: 20)).foregroundStyle(.secondary).frame(width: 28)
        }
    }
}

struct ContentView: View {
    @ObservedObject var model: CameraModel
    @State private var rawPreview = false
    @State private var resetID = 0
    @State private var roll = 1
    @State private var selectedMode = "panorama"
    let accent = Color(red: 0.94, green: 0.39, blue: 0.14)
    var active: Bool { model.phase == "connected" }
    var working: Bool { model.phase == "connecting" || model.phase == "disconnecting" }
    var body: some View {
        ScrollView {
        VStack(alignment: .leading, spacing: 22) {
            HStack(spacing: 15) {
                ZStack {
                    RoundedRectangle(cornerRadius: 17).fill(Color(red: 0.13, green: 0.14, blue: 0.16)).frame(width: 62, height: 62)
                    Image(systemName: "camera.fill").font(.system(size: 27, weight: .medium)).foregroundStyle(.white)
                    Circle().fill(accent).frame(width: 9, height: 9).offset(x: 20, y: -20)
                }
                VStack(alignment: .leading, spacing: 5) {
                    Text("影石助手").font(.system(size: 26, weight: .bold))
                    Text("无线素材 · 全景取景 · 拍摄控制").font(.system(size: 13)).foregroundStyle(.secondary)
                }
                Spacer()
                HStack(spacing: 6) {
                    Circle().fill(active ? Color.green : Color.secondary.opacity(0.5)).frame(width: 6, height: 6)
                    Text(active ? "已连接" : working ? "准备中" : "未连接").font(.system(size: 12, weight: .medium))
                }.padding(.horizontal, 11).padding(.vertical, 7).background(Color.primary.opacity(0.045), in: Capsule())
            }
            if !active { VStack(alignment: .leading, spacing: 20) {
                Text("连接前，先完成这两步").font(.system(size: 12, weight: .medium)).foregroundStyle(.secondary)
                SetupStep(number: "1", title: "在 X5 设置中打开 Wi-Fi", detail: "打开相机设置，开启 Wi-Fi，并查看相机的网络名称和密码。", symbol: "camera")
                Divider().padding(.leading, 41)
                SetupStep(number: "2", title: "在 Mac 上手动连接相机 Wi-Fi", detail: "打开 Mac 的 Wi-Fi 菜单，选择 X5 的网络并输入密码。连接后，再点击下方按钮。", symbol: "wifi")
                Button(action: model.openWiFi) {
                    Label("打开 Mac Wi-Fi 设置", systemImage: "arrow.up.right.square")
                }.buttonStyle(.link).font(.system(size: 12)).padding(.leading, 41)
            }.padding(22).background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 16))
                .overlay(RoundedRectangle(cornerRadius: 16).stroke(Color.primary.opacity(0.065), lineWidth: 1))
            } else { liveView }
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .top, spacing: 10) {
                    if working { ProgressView().controlSize(.small).padding(.top, 2) }
                    else { Image(systemName: model.error ? "exclamationmark.circle.fill" : active ? "checkmark.circle.fill" : "info.circle")
                        .foregroundStyle(model.error ? Color.orange : active ? Color.green : Color.secondary).padding(.top, 2) }
                    Text(model.message).font(.system(size: 13)).lineSpacing(3).fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("connection-status")
                }.frame(minHeight: 38, alignment: .top)
                if active {
                    HStack(spacing: 22) {
                        stat("\(model.fileCount)", "个素材文件")
                        stat(String(format: "%.1f", model.speed), "MiB/s · 小样本读取")
                        stat(String(format: "%.0f", model.cached), "MiB · 临时缓存")
                    }.padding(.bottom, 2)
                    HStack(spacing: 10) {
                        Button(action: model.openFiles) { Label("在访达中打开", systemImage: "folder").frame(maxWidth: .infinity) }
                            .buttonStyle(.borderedProminent).tint(accent).controlSize(.large)
                        Button("打开 Studio", action: model.openStudio).controlSize(.large)
                        Button("断开相机", action: model.disconnect).controlSize(.large)
                            .disabled(model.recordState == "recording" || model.controlBusy || model.previewBusy)
                    }
                } else {
                    Button(action: model.connect) {
                        Label(working ? "正在处理…" : model.error ? "重新连接相机" : "连接相机", systemImage: "link")
                            .font(.system(size: 15, weight: .semibold)).frame(maxWidth: .infinity).padding(.vertical, 7)
                    }.buttonStyle(.borderedProminent).tint(accent).controlSize(.large)
                        .disabled(working || model.running).accessibilityIdentifier("connect-camera")
                }
            }
            Spacer(minLength: 0)
            HStack(alignment: .bottom) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Wi-Fi 连接相机时，可通过手机 USB 网络共享同时上网。")
                    Text("保持相机开机；关闭 Studio 中的素材后，再安全断开。")
                }.font(.system(size: 11)).foregroundStyle(.secondary)
                Spacer()
                Button("查看日志", action: model.openLogs).buttonStyle(.link).font(.system(size: 11))
            }
        }.padding(30)
        }.frame(minWidth: 760, minHeight: 760)
            .background(Color(nsColor: .windowBackgroundColor))
            .onChange(of: model.cameraMode) { _, mode in if !mode.isEmpty { selectedMode = mode } }
    }
    var liveView: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("实时画面").font(.system(size: 15, weight: .semibold))
                Text(rawPreview ? "原始画面" : model.sensor == 3 ? "360° 拖动取景" : "单镜头取景").font(.system(size: 10)).foregroundStyle(.secondary)
                Spacer()
                Label(model.recordState == "recording" ? "录像中" : model.recordState == "idle" ? "待机" : model.recordState == "busy" ? "相机忙碌" : "拍摄状态未确认",
                      systemImage: model.recordState == "recording" ? "record.circle.fill" : "circle")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(model.recordState == "recording" ? Color.red : Color.secondary)
            }
            ZStack {
                RoundedRectangle(cornerRadius: 12).fill(Color.black.opacity(0.94))
                if let image = model.previewImage {
                    PanoramaView(image: image, raw: rawPreview, singleLens: model.sensor == 1 || model.sensor == 2,
                                 resetID: resetID, roll: model.sensor == 2 ? -roll : roll).clipShape(RoundedRectangle(cornerRadius:12))
                } else {
                    VStack(spacing: 12) {
                        if model.previewState == "starting" { ProgressView().colorScheme(.dark) }
                        else { Image(systemName: "video").font(.system(size: 28)).foregroundStyle(.white.opacity(0.65)) }
                        Text(model.previewMessage).font(.system(size: 12)).foregroundStyle(.white.opacity(0.75))
                            .multilineTextAlignment(.center).frame(maxWidth: 410)
                    }.padding(20)
                }
            }.frame(height: 310).clipped().accessibilityLabel("相机实时预览")
            HStack(spacing: 12) {
                Toggle("原始画面", isOn: $rawPreview).toggleStyle(.checkbox)
                Button("视角回正") { resetID += 1 }
                Button("旋转 90°") { roll = (roll + 1) % 4 }
                Spacer()
                Text("拖动转向 · 滚动 / 捏合缩放 · 双击回正").foregroundStyle(.secondary)
            }.font(.system(size:11)).disabled(model.previewImage == nil)
            Text("实时取景使用基础鱼眼投影，接缝处可能重影；不会改变相机保存的原片或云台跟拍目标。")
                .font(.system(size:10)).foregroundStyle(.secondary)
            HStack(spacing: 12) {
                Picker("拍摄模式", selection: $selectedMode) {
                    ForEach(model.cameraModes.indices, id: \.self) { index in
                        Text(model.cameraModes[index]["label"] ?? "").tag(model.cameraModes[index]["id"] ?? "")
                    }
                }.frame(maxWidth: 330)
                Button("应用模式") { model.control("mode_set", mode: selectedMode) }
                    .disabled(model.cameraModes.isEmpty)
                Spacer()
            }.disabled(model.controlBusy || model.previewBusy || model.recordState != "idle")
            Text("当前：\(model.cameraModeLabel)").font(.system(size:11)).foregroundStyle(.secondary)
            Text(model.connectionHealth).font(.system(size:11)).foregroundStyle(.secondary)
            HStack(spacing: 10) {
                Button { model.control(model.previewState == "on" || model.previewState == "starting" ? "preview_stop" : "preview_start") } label: {
                    Label(model.previewState == "on" || model.previewState == "starting" ? "关闭预览" : "开启预览", systemImage: "video")
                }.disabled(model.previewBusy || model.controlBusy)
                Spacer()
                Button("开始录像") { model.control("record_start") }
                    .buttonStyle(.borderedProminent).tint(.red)
                    .disabled(model.controlBusy || model.previewBusy || model.recordState != "idle")
                Button("停止录像") { model.control("record_stop") }
                    .disabled(model.controlBusy || model.previewBusy || model.recordState == "idle" || model.recordState == "busy")
                Button { model.control("status") } label: { Image(systemName: "arrow.clockwise") }
                    .help("同步相机拍摄状态").accessibilityLabel("同步状态")
                    .disabled(model.controlBusy || model.previewBusy)
            }.controlSize(.large)
            Text(model.controlMessage).font(.system(size: 11)).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true).frame(minHeight: 28, alignment: .top)
        }
    }
    func stat(_ value: String, _ label: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(value).font(.system(size: 20, weight: .semibold, design: .rounded))
            Text(label).font(.system(size: 10)).foregroundStyle(.secondary)
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let model = CameraModel()
    var window: NSWindow!
    func applicationDidFinishLaunching(_ notification: Notification) {
        let menu = NSMenu(); let appItem = NSMenuItem(); let submenu = NSMenu()
        submenu.addItem(withTitle: "关于影石助手", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        submenu.addItem(.separator())
        submenu.addItem(withTitle: "退出影石助手", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = submenu; menu.addItem(appItem); NSApp.mainMenu = menu
        window = NSWindow(contentRect: NSRect(x:0,y:0,width:850,height:850), styleMask:[.titled,.closable,.miniaturizable,.resizable], backing:.buffered, defer:false)
        window.minSize = NSSize(width:760,height:760)
        window.title = "影石助手"; window.titlebarAppearsTransparent = true
        window.contentView = NSHostingView(rootView: ContentView(model: model))
        window.delegate = self; window.isReleasedWhenClosed = false
        window.center(); window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps:true)
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if !model.running { return .terminateNow }
        if model.quitting { return .terminateCancel }
        if model.recordState == "recording" || model.controlBusy {
            let alert = NSAlert(); alert.messageText = "请先停止录像"
            alert.informativeText = "请点击“停止录像”，等待相机完成保存后再退出。"
            alert.addButton(withTitle: "继续使用"); alert.runModal(); return .terminateCancel
        }
        let alert = NSAlert(); alert.messageText = "先断开相机，再退出"
        alert.informativeText = "请先关闭 Studio 中正在使用的素材。断开连接不会停止相机录像；若拍摄状态未确认，请检查相机屏幕。临时缓存会自动清理。"
        alert.addButton(withTitle: "断开并退出"); alert.addButton(withTitle: "继续使用")
        if alert.runModal() == .alertFirstButtonReturn { model.quitting = true; model.disconnect() }
        return .terminateCancel
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool { NSApp.terminate(nil); return false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil);return true
    }
}

@main struct Main {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.setActivationPolicy(.regular);app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
