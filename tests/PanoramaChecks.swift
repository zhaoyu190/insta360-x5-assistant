import AppKit
import MetalKit

// Run on macOS: swiftc app/PanoramaView.swift tests/PanoramaChecks.swift
// -framework AppKit -framework SwiftUI -framework MetalKit -o /tmp/panorama-check
// Synthetic pixels only: never needs a camera or private footage.
@main struct PanoramaChecks {
    static func main() throws {
        guard let device=MTLCreateSystemDefaultDevice(), let queue=device.makeCommandQueue() else {
            fatalError("Metal unavailable")
        }
        let library=try device.makeLibrary(source:panoramaShader,options:nil)
        let description=MTLRenderPipelineDescriptor()
        description.vertexFunction=library.makeFunction(name:"panoramaVertex")
        description.fragmentFunction=library.makeFunction(name:"panoramaFragment")
        description.colorAttachments[0].pixelFormat = .rgba8Unorm
        let pipeline=try device.makeRenderPipelineState(descriptor:description)
        let inputDescription=MTLTextureDescriptor.texture2DDescriptor(pixelFormat:.rgba8Unorm,width:256,height:128,mipmapped:false)
        inputDescription.storageMode = .shared
        let input=device.makeTexture(descriptor:inputDescription)!
        var pixels=[UInt8](repeating:0,count:256*128*4)
        for y in 0..<128 { for x in 0..<256 {
            let i=(y*256+x)*4
            pixels[i]=x<128 ? 255 : 0; pixels[i+1]=UInt8(y*2)
            pixels[i+2]=x<128 ? 0 : 255; pixels[i+3]=255
        } }
        input.replace(region:MTLRegionMake2D(0,0,256,128),mipmapLevel:0,withBytes:pixels,bytesPerRow:256*4)
        func render(yaw:Float=0,pitch:Float=0,fov:Float=75,projection:Float=0,roll:Float=0) throws -> [UInt8] {
            let outputDescription=MTLTextureDescriptor.texture2DDescriptor(pixelFormat:.rgba8Unorm,width:64,height:64,mipmapped:false)
            outputDescription.storageMode = .shared; outputDescription.usage=[.renderTarget,.shaderRead]
            let output=device.makeTexture(descriptor:outputDescription)!
            let pass=MTLRenderPassDescriptor()
            pass.colorAttachments[0].texture=output; pass.colorAttachments[0].loadAction = .clear
            pass.colorAttachments[0].storeAction = .store
            let command=queue.makeCommandBuffer()!
            let encoder=command.makeRenderCommandEncoder(descriptor:pass)!
            var params:[Float]=[yaw,pitch,fov*Float.pi/180,1,projection,roll,200*Float.pi/180,0]
            encoder.setRenderPipelineState(pipeline);encoder.setFragmentTexture(input,index:0)
            encoder.setFragmentBytes(&params,length:params.count*4,index:0)
            encoder.drawPrimitives(type:.triangle,vertexStart:0,vertexCount:3)
            encoder.endEncoding();command.commit();command.waitUntilCompleted()
            if let error=command.error { throw error }
            var result=[UInt8](repeating:0,count:64*64*4)
            output.getBytes(&result,bytesPerRow:64*4,from:MTLRegionMake2D(0,0,64,64),mipmapLevel:0)
            return result
        }
        let center=(32*64+32)*4
        let front=try render(),rear=try render(yaw:Float.pi),seam=try render(yaw:Float.pi/2)
        precondition(front[center]>240 && front[center+2]<10,"Front lens mapping")
        precondition(rear[center]<10 && rear[center+2]>240,"Rear lens mapping")
        precondition(seam[center]>20 && seam[center+2]>20,"Seam blend")
        let up=try render(pitch:0.5),wide=try render(fov:110)
        precondition(up[center+1]<front[center+1],"Pitch direction")
        let upperPixel: Int = 2177
        precondition(wide[upperPixel]<front[upperPixel],"Zoom changes projection")
        let raw=try render(projection:2)
        let leftPixel: Int = 8224
        let rightPixel: Int = 8418
        precondition(raw[leftPixel]>240 && raw[rightPixel]>240,"Raw bypass")
        let frontTurn=try render(yaw:0.5,roll:Float.pi/2)
        let rearTurn=try render(yaw:Float.pi+0.5,roll:Float.pi/2)
        precondition(frontTurn[center+1]<front[center+1],"Front orientation")
        precondition(rearTurn[center+1]>rear[center+1],"Rear orientation")
        precondition(raw[upperPixel]==8 || raw[upperPixel]==7,"Raw preserves aspect ratio with letterboxing")
        let single=try render(projection:3,roll:Float.pi/2)
        precondition(single[leftPixel]==8 || single[leftPixel]==7,"Single lens rotates with pillarboxing")
        print("PASS: real Metal shader, front/back, seam blend, pitch, zoom, raw and single aspect ratio, lens orientation")
    }
}
