#!/usr/bin/env swift
import AppKit
import Foundation

struct IconError: Error, CustomStringConvertible {
    let description: String
}

let arguments = Array(CommandLine.arguments.dropFirst())
guard arguments.count == 4, arguments[0] == "--channel",
      ["play", "lab"].contains(arguments[1]), arguments[2] == "--output" else {
    fputs("Usage: swift tools/tvos_icons.swift --channel play|lab --output DIR\n", stderr)
    exit(2)
}
let channel = arguments[1]
let output = URL(fileURLWithPath: arguments[3], isDirectory: true)
let fileManager = FileManager.default
let canvas = CGRect(x: 0, y: 0, width: 1280, height: 768)
let isLab = channel == "lab"
let accent = isLab ? NSColor(srgbRed: 0.38, green: 0.96, blue: 1, alpha: 1)
                   : NSColor(srgbRed: 1, green: 0.79, blue: 0.36, alpha: 1)
let background = isLab ? NSColor(srgbRed: 0.15, green: 0.05, blue: 0.28, alpha: 1)
                       : NSColor(srgbRed: 0.025, green: 0.07, blue: 0.16, alpha: 1)
let metadata: [String: Any] = ["info": ["author": "xcode", "version": 1]]

func writeContents(_ directory: URL, _ fields: [String: Any] = [:]) throws {
    try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
    let content = metadata.merging(fields) { _, new in new }
    let data = try JSONSerialization.data(withJSONObject: content, options: [.prettyPrinted, .sortedKeys])
    try data.write(to: directory.appendingPathComponent("Contents.json"), options: .atomic)
}

func polygon(_ points: [CGPoint], color: NSColor) {
    let path = NSBezierPath()
    path.move(to: points[0])
    for point in points.dropFirst() { path.line(to: point) }
    path.close()
    color.setFill()
    path.fill()
}

func text(_ value: String, y: CGFloat, size: CGFloat, color: NSColor, weight: NSFont.Weight) {
    let attributes: [NSAttributedString.Key: Any] = [
        .font: NSFont.systemFont(ofSize: size, weight: weight),
        .foregroundColor: color,
        .kern: size * 0.06,
    ]
    let string = NSAttributedString(string: value, attributes: attributes)
    let dimensions = string.size()
    string.draw(at: CGPoint(x: (canvas.width - dimensions.width) / 2, y: y))
}

func drawLayer(_ name: String) {
    if name == "Back" {
        background.setFill()
        canvas.fill()
        let glow = NSGradient(starting: accent.withAlphaComponent(0.24), ending: background)!
        glow.draw(in: NSBezierPath(ovalIn: CGRect(x: 130, y: 70, width: 1020, height: 810)),
                  relativeCenterPosition: NSPoint(x: 0, y: 0.15))
        for x in stride(from: 90, through: 1190, by: 110) {
            let path = NSBezierPath()
            path.move(to: CGPoint(x: x, y: 0))
            path.line(to: CGPoint(x: 640 + (x - 640) / 3, y: 470))
            accent.withAlphaComponent(0.07).setStroke()
            path.lineWidth = 2
            path.stroke()
        }
    } else if name == "Middle" {
        let outer = [CGPoint(x: 465, y: 368), CGPoint(x: 465, y: 516),
                     CGPoint(x: 640, y: 638), CGPoint(x: 815, y: 516), CGPoint(x: 815, y: 368)]
        let inner = [CGPoint(x: 497, y: 368), CGPoint(x: 497, y: 499),
                     CGPoint(x: 640, y: 598), CGPoint(x: 783, y: 499), CGPoint(x: 783, y: 368)]
        polygon(outer, color: accent)
        polygon(inner, color: background)
        polygon([CGPoint(x: 465, y: 368), CGPoint(x: 640, y: 402), CGPoint(x: 815, y: 368),
                 CGPoint(x: 640, y: 342)], color: accent.withAlphaComponent(0.55))
        let moon = NSBezierPath(ovalIn: CGRect(x: 590, y: 455, width: 100, height: 100))
        accent.setFill()
        moon.fill()
        background.setFill()
        NSBezierPath(ovalIn: CGRect(x: 619, y: 477, width: 85, height: 85)).fill()
        polygon([CGPoint(x: 640, y: 425), CGPoint(x: 650, y: 440),
                 CGPoint(x: 640, y: 455), CGPoint(x: 630, y: 440)], color: accent)
    } else {
        text("DUSKLIGHT", y: 255, size: 72, color: .white, weight: .bold)
        let badge = CGRect(x: 460, y: 130, width: 360, height: 94)
        accent.setFill()
        NSBezierPath(roundedRect: badge, xRadius: 18, yRadius: 18).fill()
        text(channel.uppercased(), y: 147, size: 52, color: background, weight: .heavy)
    }
}

func render(width: Int, height: Int, layers: [String], to destination: URL) throws {
    guard let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: width,
                                      pixelsHigh: height, bitsPerSample: 8, samplesPerPixel: 4,
                                      hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                      bytesPerRow: 0, bitsPerPixel: 0),
          let context = NSGraphicsContext(bitmapImageRep: bitmap) else {
        throw IconError(description: "Could not allocate icon bitmap")
    }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = context
    context.cgContext.clear(CGRect(x: 0, y: 0, width: width, height: height))
    context.cgContext.scaleBy(x: CGFloat(width) / canvas.width, y: CGFloat(height) / canvas.height)
    for layer in layers { drawLayer(layer) }
    context.flushGraphics()
    NSGraphicsContext.restoreGraphicsState()
    guard let png = bitmap.representation(using: .png, properties: [:]) else {
        throw IconError(description: "Could not encode icon PNG")
    }
    try png.write(to: destination, options: .atomic)
}

do {
    let catalog = output.appendingPathComponent("Assets.xcassets", isDirectory: true)
    let brand = catalog.appendingPathComponent("AppIcon.brandassets", isDirectory: true)
    try writeContents(catalog)
    try writeContents(brand, ["assets": [
        ["filename": "Small.imagestack", "idiom": "tv", "size": "400x240", "role": "primary-app-icon"],
        ["filename": "Large.imagestack", "idiom": "tv", "size": "1280x768", "role": "primary-app-icon"],
    ]])
    let layerNames = ["Front", "Middle", "Back"]
    for (name, width, height, scales) in [("Small", 400, 240, [1, 2]), ("Large", 1280, 768, [1])] {
        let stack = brand.appendingPathComponent("\(name).imagestack", isDirectory: true)
        try writeContents(stack, ["layers": layerNames.map { ["filename": "\($0).imagestacklayer"] }])
        for layerName in layerNames {
            let layer = stack.appendingPathComponent("\(layerName).imagestacklayer", isDirectory: true)
            let images = layer.appendingPathComponent("Content.imageset", isDirectory: true)
            try writeContents(layer)
            try writeContents(images, ["images": scales.map {
                ["filename": "\($0)x.png", "idiom": "tv", "scale": "\($0)x"]
            }])
            for scale in scales {
                try render(width: width * scale, height: height * scale, layers: [layerName],
                           to: images.appendingPathComponent("\(scale)x.png"))
            }
        }
    }
    try render(width: 1280, height: 768, layers: layerNames.reversed(),
               to: output.appendingPathComponent("preview.png"))
    print("Generated \(channel) icons in \(output.path)")
} catch {
    fputs("tvos-icons: \(error)\n", stderr)
    exit(1)
}
