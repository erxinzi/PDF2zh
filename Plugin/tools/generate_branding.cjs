/* Deterministic icon conversion from the user's original image, preserving its proportions. */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const root = path.resolve(__dirname, '..');
const { createCanvas, loadImage } = require(path.join(root, 'node_modules/@napi-rs/canvas'));
const directory = path.join(root, 'assets/branding');
const pluginIcons = path.join(root, 'addon/content/icons');
const hash = buffer => crypto.createHash('sha256').update(buffer).digest('hex');

async function main() {
    const source = fs.readFileSync(path.join(directory, 'logo.jpg'));
    const original = await loadImage(source);
    fs.mkdirSync(pluginIcons, { recursive: true });
    const sizes = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256, 512, 1024];
    const images = new Map();
    for (const size of sizes) {
        const canvas = createCanvas(size, size);
        const context = canvas.getContext('2d');
        context.imageSmoothingEnabled = true;
        context.imageSmoothingQuality = 'high';
        const ratio = Math.min(size / original.width, size / original.height);
        const width = original.width * ratio, height = original.height * ratio;
        context.drawImage(original, (size - width) / 2, (size - height) / 2, width, height);
        const png = canvas.toBuffer('image/png');
        images.set(size, png);
        fs.writeFileSync(path.join(directory, `logo-${size}.png`), png);
    }
    const frames = sizes.filter(size => size <= 256);
    const header = Buffer.alloc(6 + frames.length * 16);
    header.writeUInt16LE(1, 2); header.writeUInt16LE(frames.length, 4);
    let offset = header.length;
    frames.forEach((size, index) => {
        const start = 6 + index * 16, png = images.get(size);
        header[start] = header[start + 1] = size === 256 ? 0 : size;
        header.writeUInt16LE(1, start + 4); header.writeUInt16LE(32, start + 6);
        header.writeUInt32LE(png.length, start + 8); header.writeUInt32LE(offset, start + 12);
        offset += png.length;
    });
    const ico = Buffer.concat([header, ...frames.map(size => images.get(size))]);
    fs.writeFileSync(path.join(directory, 'app.ico'), ico);
    fs.writeFileSync(path.join(pluginIcons, 'favicon.png'), images.get(96));
    fs.writeFileSync(path.join(pluginIcons, 'favicon@0.5x.png'), images.get(48));
    // Retain legacy asset paths with the same new artwork for older consumers.
    const svg = size => `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}"><image width="${size}" height="${size}" href="data:image/png;base64,${images.get(size).toString('base64')}"/></svg>\n`;
    fs.writeFileSync(path.join(pluginIcons, 'favicon.svg'), svg(96));
    fs.writeFileSync(path.join(pluginIcons, 'favicon@0.5x.svg'), svg(48));
    const manifest = { source: 'logo.jpg', source_sha256: hash(source),
        original_width: original.width, original_height: original.height,
        ico_sha256: hash(ico), ico_sizes: frames,
        png_sha256: Object.fromEntries(sizes.map(size => [String(size), hash(images.get(size))])) };
    fs.writeFileSync(path.join(directory, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
    process.stdout.write(`PDF2zh branding generated from ${original.width} x ${original.height}; ${frames.length} ICO sizes.\n`);
}
main().catch(error => { process.stderr.write(String(error) + '\n'); process.exitCode = 1; });
