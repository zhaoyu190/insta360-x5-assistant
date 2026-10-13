import AppKit
import MetalKit
import SwiftUI

// Project the live dual-fisheye texture directly on the GPU. No panorama copy is
// made on each drag: the most recent frame remains a texture while the view moves.
let panoramaShader = """
#include <metal_stdlib>
using namespace metal;
struct Vertex { float4 position [[position]]; float2 uv; };
struct ViewParams { float yaw, pitch, fov, aspect; float projection, roll, lensFOV, unused; };
vertex Vertex panoramaVertex(uint index [[vertex_id]]) {
    float2 positions[3] = {float2(-1,-1), float2(3,-1), float2(-1,3)};
    float2 p = positions[index];
    return {float4(p,0,1), float2((p.x+1)*0.5, (1-p.y)*0.5)};
}
float2 lensUV(float3 d, bool rear, bool single, constant ViewParams& p) {
    float angle = acos(clamp(rear ? -d.z : d.z, -1.0f, 1.0f));
    float radius = angle / (p.lensFOV * 0.5);
    float2 plane = float2(rear ? -d.x : d.x, d.y);
    float lengthXY = length(plane);
    plane = lengthXY > 0.00001 ? plane / lengthXY * radius : float2(0);
    float angleRoll=rear ? -p.roll : p.roll;
    float c=cos(angleRoll), s=sin(angleRoll);
    plane=float2(c*plane.x-s*plane.y,s*plane.x+c*plane.y);
    return float2(single ? 0.5 : (rear ? 0.75 : 0.25), 0.5)
        + plane * float2(single ? 0.5 : 0.25, -0.5);
}
fragment float4 panoramaFragment(Vertex in [[stage_in]],
                                texture2d<float> picture [[texture(0)]],
                                constant ViewParams& p [[buffer(0)]]) {
    constexpr sampler linearSampler(coord::normalized, address::clamp_to_edge, filter::linear);
    if (p.projection > 1.5) {
        float angle=p.projection > 2.5 ? p.roll : 0;
        float c=cos(angle),s=sin(angle);
        float aspect=float(picture.get_width())/float(picture.get_height());
        float rotatedAspect=abs(c)>0.5 ? aspect : 1/aspect;
        float2 point=float2(in.uv.x*2-1,1-in.uv.y*2);
        if (p.aspect > rotatedAspect) point.x*=p.aspect/rotatedAspect;
        else point.y*=rotatedAspect/p.aspect;
        point=float2(c*point.x-s*point.y,s*point.x+c*point.y);
        if (any(abs(point)>1)) return float4(0.03,0.03,0.03,1);
        return picture.sample(linearSampler,float2(point.x+1,1-point.y)*0.5);
    }
    float tangent=tan(p.fov*0.5);
    float3 d=normalize(float3((in.uv.x*2-1)*tangent*p.aspect,(1-in.uv.y*2)*tangent,1));
    float cp=cos(p.pitch), sp=sin(p.pitch), cy=cos(p.yaw), sy=sin(p.yaw);
    d=float3(d.x,cp*d.y+sp*d.z,-sp*d.y+cp*d.z);
    d=float3(cy*d.x+sy*d.z,d.y,-sy*d.x+cy*d.z);
    if (p.projection > 0.5) {
        if (acos(clamp(d.z,-1.0f,1.0f)) > p.lensFOV*0.5) return float4(0.04,0.04,0.04,1);
        return picture.sample(linearSampler,lensUV(d,false,true,p));
    }
    float4 front=picture.sample(linearSampler,lensUV(d,false,false,p));
    float4 rear=picture.sample(linearSampler,lensUV(d,true,false,p));
    return mix(rear,front,smoothstep(-0.06f,0.06f,d.z));
}
"""

struct PanoramaView: NSViewRepresentable {
    let image: NSImage
    let raw: Bool
    let singleLens: Bool
    let resetID: Int
    let roll: Int
    func makeNSView(context: Context) -> PanoramaSurface { PanoramaSurface() }
    func updateNSView(_ view: PanoramaSurface, context: Context) {
        view.update(image: image, projection: raw ? 2 : singleLens ? 3 : 0, resetID: resetID, roll: roll)
    }
}

final class PanoramaSurface: MTKView, MTKViewDelegate {
    var commandQueue: MTLCommandQueue?
    var pipeline: MTLRenderPipelineState?
    var picture: MTLTexture?
    var lastImage: NSImage?
    var yaw: Float = 0
    var pitch: Float = 0
    var fieldOfView: Float = 75
    var projection: Float = 0
    var roll: Float = 0
    var lastReset = 0
    var dragPoint: NSPoint?
    var failure: String?

    init() {
        super.init(frame: .zero, device: MTLCreateSystemDefaultDevice())
        colorPixelFormat = .bgra8Unorm
        clearColor = MTLClearColorMake(0.03,0.03,0.03,1)
        isPaused = true; enableSetNeedsDisplay = true
        delegate = self
        guard let device = device else { showFailure("此 Mac 无法初始化 Metal 预览。"); return }
        do {
            commandQueue = device.makeCommandQueue()
            let library = try device.makeLibrary(source: panoramaShader, options: nil)
            let description = MTLRenderPipelineDescriptor()
            description.vertexFunction = library.makeFunction(name: "panoramaVertex")
            description.fragmentFunction = library.makeFunction(name: "panoramaFragment")
            description.colorAttachments[0].pixelFormat = colorPixelFormat
            pipeline = try device.makeRenderPipelineState(descriptor: description)
        } catch { showFailure("全景渲染初始化失败：\(error.localizedDescription)") }
        setAccessibilityLabel("全景实时画面，可拖动旋转、滚动缩放、双击回正")
    }
    required init(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    func showFailure(_ text: String) {
        failure = text
        let label=NSTextField(wrappingLabelWithString: text)
        label.textColor = .white; label.translatesAutoresizingMaskIntoConstraints=false
        addSubview(label)
        NSLayoutConstraint.activate([label.centerXAnchor.constraint(equalTo:centerXAnchor),
            label.centerYAnchor.constraint(equalTo:centerYAnchor),label.widthAnchor.constraint(lessThanOrEqualTo:widthAnchor,constant:-32)])
    }
    func update(image: NSImage, projection: Int, resetID: Int, roll: Int) {
        if self.projection != Float(projection) || lastReset != resetID {
            yaw=0; pitch=0; fieldOfView=75; lastReset=resetID
        }
        self.projection=Float(projection); self.roll=Float(roll)*Float.pi/2
        if lastImage !== image, let device = device {
            var rectangle = NSRect(origin: .zero, size: image.size)
            if let cg=image.cgImage(forProposedRect: &rectangle, context: nil, hints: nil) {
                do {
                    picture=try MTKTextureLoader(device:device).newTexture(cgImage:cg,options:[.SRGB:false,.origin:MTKTextureLoader.Origin.topLeft])
                    lastImage=image
                } catch { if failure == nil { showFailure("无法载入预览画面。") } }
            }
        }
        needsDisplay=true
    }
    override var acceptsFirstResponder: Bool { true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func mouseDown(with event: NSEvent) {
        window?.makeFirstResponder(self)
        dragPoint = convert(event.locationInWindow, from: nil)
        if event.clickCount == 2 { yaw=0; pitch=0; fieldOfView=75; needsDisplay=true }
    }
    override func mouseDragged(with event: NSEvent) {
        guard projection < 1.5 else { return }
        let point = convert(event.locationInWindow, from: nil)
        guard let previous = dragPoint else { dragPoint=point; return }
        dragPoint=point
        yaw -= Float(point.x-previous.x)*0.006
        pitch=max(-1.48,min(1.48,pitch-Float(point.y-previous.y)*0.006))
        if projection > 0.5 { yaw=max(-0.8,min(0.8,yaw)) }
        else { yaw=yaw.truncatingRemainder(dividingBy:Float.pi*2) }
        needsDisplay=true
    }
    override func mouseUp(with event: NSEvent) {
        mouseDragged(with: event); dragPoint=nil
    }
    override func scrollWheel(with event: NSEvent) {
        fieldOfView=max(35,min(110,fieldOfView+Float(event.scrollingDeltaY)*0.25))
        needsDisplay=true
    }
    override func magnify(with event: NSEvent) {
        fieldOfView=max(35,min(110,fieldOfView*(1-Float(event.magnification))))
        needsDisplay=true
    }
    func mtkView(_ view: MTKView, drawableSizeWillChange size: CGSize) { needsDisplay=true }
    func draw(in view: MTKView) {
        guard let drawable=currentDrawable, let pass=currentRenderPassDescriptor,
              let pipeline=pipeline, let texture=picture,
              let command=commandQueue?.makeCommandBuffer(), let encoder=command.makeRenderCommandEncoder(descriptor:pass) else { return }
        var params:[Float]=[yaw,pitch,fieldOfView*Float.pi/180,Float(max(1,drawableSize.width)/max(1,drawableSize.height)),
                            projection,roll,200*Float.pi/180,0]
        encoder.setRenderPipelineState(pipeline)
        encoder.setFragmentTexture(texture,index:0)
        encoder.setFragmentBytes(&params,length:MemoryLayout<Float>.size*params.count,index:0)
        encoder.drawPrimitives(type:.triangle,vertexStart:0,vertexCount:3)
        encoder.endEncoding(); command.present(drawable); command.commit()
    }
}
