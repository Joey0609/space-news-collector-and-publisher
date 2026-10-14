"""排除二维码区域超过整张图片50%的图片；不解码或记录二维码内容。"""
from functools import lru_cache
from pathlib import Path
from collect.paths import LOG_DIR
from collect.logging_util import make_log

log = make_log(LOG_DIR / 'image_filter.log')


@lru_cache(maxsize=2048)
def _qr_fraction(path, modified, size):
    import cv2
    import numpy as np
    # imdecode 支持 Windows 中文文件路径；缩放只用于定位，不改变原图。
    image = cv2.imdecode(np.frombuffer(Path(path).read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return None
    height, width = image.shape[:2]
    scale = min(1.0, 1600 / max(height, width))
    if scale < 1:
        image = cv2.resize(image, (round(width * scale), round(height * scale)))
    height, width = image.shape[:2]
    mask = np.zeros((height, width), dtype=np.uint8)
    polygons = []
    detectors = [cv2.QRCodeDetectorAruco(), cv2.QRCodeDetector()]
    # 白边也能帮助定位占满画面、没有完整留白的二维码。
    border = max(16, round(min(width, height) * .05))
    for detector in detectors:
        for padding in (0, border):
            sample = image if not padding else cv2.copyMakeBorder(image, padding, padding, padding, padding, cv2.BORDER_CONSTANT, value=(255, 255, 255))
            found, points = detector.detectMulti(sample)
            if not found:
                found, points = detector.detect(sample)
            if found and points is not None:
                for polygon in points.reshape(-1, 4, 2):
                    polygon = polygon - padding
                    polygon[:, 0] = np.clip(polygon[:, 0], 0, width - 1)
                    polygon[:, 1] = np.clip(polygon[:, 1], 0, height - 1)
                    polygons.append(polygon)
                    cv2.fillConvexPoly(mask, np.rint(polygon).astype(np.int32), 255)
    if not polygons:
        return 0.0
    # 多次检测到同一个码不能重复累加面积；取二维码区域并集。
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    fraction = sum(cv2.contourArea(contour) for contour in contours) / (width * height)
    if fraction > .5:
        log(f'[图片排除] {Path(path).name} 二维码面积占比={fraction:.1%}，超过50%')
    return fraction


def filter_image_paths(paths):
    if not isinstance(paths, (list, tuple)):
        paths = [paths] if isinstance(paths, str) and paths.strip() else []
    kept = []
    for value in paths:
        path = str(value).strip()
        if not path or path in kept:
            continue
        try:
            file = Path(path)
            if not file.is_file():
                kept.append(path)
                continue
            stat = file.stat()
            fraction = _qr_fraction(str(file.resolve()), stat.st_mtime_ns, stat.st_size)
            if fraction is not None and fraction > .5:
                continue
        except Exception as exc:
            log(f'[图片识别失败] {Path(path).name} 类型={type(exc).__name__}，保留原图')
        kept.append(path)
    return kept
