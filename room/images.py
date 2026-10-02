"""Bounded validation of browser canvas PNG captures, without imaging dependencies."""
import struct
import zlib

SIGNATURE = b"\x89PNG\r\n\x1a\n"


def validate_png(data):
    if not data.startswith(SIGNATURE) or len(data) > 4 * 1024 * 1024:
        raise ValueError("照片必须是 4MB 以内的 PNG")
    offset, chunks, pixels, size = 8, [], bytearray(), None
    ended = False
    idat_done = False
    while offset < len(data):
        if offset + 12 > len(data):
            raise ValueError("PNG 数据不完整")
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        end = offset + 12 + length
        if end > len(data):
            raise ValueError("PNG 数据不完整")
        kind = data[offset + 4:offset + 8]
        content = data[offset + 8:end - 4]
        checksum = struct.unpack(">I", data[end - 4:end])[0]
        if zlib.crc32(kind + content) & 0xFFFFFFFF != checksum:
            raise ValueError("PNG 校验失败")
        if not chunks and kind != b"IHDR":
            raise ValueError("缺少 PNG 图像头")
        if kind == b"IHDR":
            if size or length != 13:
                raise ValueError("PNG 图像头无效")
            width, height, depth, color, compress, filtering, interlace = struct.unpack(">IIBBBBB", content)
            if not 1 <= width <= 1280 or not 1 <= height <= 720 or depth != 8 or color not in (2, 6) or (compress, filtering, interlace) != (0, 0, 0):
                raise ValueError("仅接受最大 1280×720 的普通 RGB/RGBA PNG")
            size = (width, height, 3 if color == 2 else 4)
        elif kind == b"IDAT":
            if idat_done:
                raise ValueError("PNG 数据块顺序错误")
            pixels.extend(content)
        elif kind == b"IEND":
            if length != 0 or end != len(data):
                raise ValueError("PNG 结束标识错误")
            ended = True
        else:
            if pixels:
                idat_done = True
            if kind[0] & 32 == 0:
                raise ValueError("不支持此 PNG 格式")
        if kind in (b"IHDR", b"IDAT", b"IEND"):
            chunks.append(data[offset:end])
        offset = end
    if not ended or not size or not pixels:
        raise ValueError("PNG 数据不完整")
    width, height, channels = size
    expected = height * (1 + width * channels)
    decoder = zlib.decompressobj()
    try:
        raw = decoder.decompress(bytes(pixels), expected + 1)
    except zlib.error as exc:
        raise ValueError("PNG 像素数据损坏") from exc
    if len(raw) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError("PNG 像素长度错误")
    stride = 1 + width * channels
    if any(raw[i] > 4 for i in range(0, len(raw), stride)):
        raise ValueError("PNG 过滤数据无效")
    return SIGNATURE + b"".join(chunks)
