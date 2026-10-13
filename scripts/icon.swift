import AppKit
let directory=CommandLine.arguments[1]
for size in [16,32,64,128,256,512,1024] {
 let image=NSImage(size:NSSize(width:size,height:size))
 image.lockFocus()
 let s=CGFloat(size)
 NSColor(calibratedRed:0.13,green:0.14,blue:0.16,alpha:1).setFill()
 NSBezierPath(roundedRect:NSRect(x:s*0.06,y:s*0.06,width:s*0.88,height:s*0.88),xRadius:s*0.19,yRadius:s*0.19).fill()
 NSColor.white.setFill()
 NSBezierPath(roundedRect:NSRect(x:s*0.21,y:s*0.25,width:s*0.58,height:s*0.43),xRadius:s*0.055,yRadius:s*0.055).fill()
 NSBezierPath(roundedRect:NSRect(x:s*0.32,y:s*0.63,width:s*0.23,height:s*0.10),xRadius:s*0.03,yRadius:s*0.03).fill()
 NSColor(calibratedRed:0.13,green:0.14,blue:0.16,alpha:1).setFill()
 NSBezierPath(ovalIn:NSRect(x:s*0.38,y:s*0.33,width:s*0.24,height:s*0.24)).fill()
 NSColor(calibratedRed:0.94,green:0.39,blue:0.14,alpha:1).setFill()
 NSBezierPath(ovalIn:NSRect(x:s*0.70,y:s*0.71,width:s*0.12,height:s*0.12)).fill()
 image.unlockFocus()
 let rep=NSBitmapImageRep(data:image.tiffRepresentation!)!
 let data=rep.representation(using:.png,properties:[:])!
 let names:[String]
 switch size {
 case 16:names=["icon_16x16.png"]
 case 32:names=["icon_16x16@2x.png","icon_32x32.png"]
 case 64:names=["icon_32x32@2x.png"]
 case 128:names=["icon_128x128.png"]
 case 256:names=["icon_128x128@2x.png","icon_256x256.png"]
 case 512:names=["icon_256x256@2x.png","icon_512x512.png"]
 default:names=["icon_512x512@2x.png"]
 }
 for name in names {try data.write(to:URL(fileURLWithPath:directory).appendingPathComponent(name))}
}
