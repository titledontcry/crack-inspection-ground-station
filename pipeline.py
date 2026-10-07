from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path

print('[pipeline] importing cv2', flush=True)
import cv2
import numpy as np

print('[pipeline] importing torch', flush=True)
import torch
print('[pipeline] torch imported', flush=True)
from PIL import Image

print('[pipeline] importing skimage', flush=True)
from skimage.morphology import remove_small_objects, skeletonize
print('[pipeline] imports complete', flush=True)

IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


@dataclass(frozen=True)
class InferenceConfig:
    checkpoint: Path
    device: str = 'auto'
    threshold: float = 0.1
    tile_size: int = 448
    tile_overlap: float = 0.25
    min_component_px: int = 20


def select_device(requested: str) -> torch.device:
    if requested != 'auto':
        return torch.device(requested)
    if torch.cuda.is_available():
        return torch.device('cuda')
    if torch.backends.mps.is_available():
        return torch.device('mps')
    return torch.device('cpu')


class CrackPredictor:
    def __init__(self, cfg: InferenceConfig):
        start = time.perf_counter()

        def log(message: str) -> None:
            print(f'[model +{time.perf_counter() - start:.1f}s] {message}', flush=True)

        log(f'Checking checkpoint: {cfg.checkpoint}')
        if not cfg.checkpoint.is_file():
            raise FileNotFoundError(f'Checkpoint not found: {cfg.checkpoint}')
        self.cfg = cfg
        log('Selecting device')
        self.device = select_device(cfg.device)
        log(f'Selected device: {self.device}')
        log('Importing segmentation_models_pytorch')
        import segmentation_models_pytorch as smp

        log('Creating U-Net ResNet34 on CPU')
        self.model = smp.Unet('resnet34', encoder_weights=None, in_channels=3, classes=1)
        log('Reading checkpoint on CPU')
        checkpoint = torch.load(cfg.checkpoint, map_location='cpu', weights_only=False)
        if not isinstance(checkpoint, dict) or 'model' not in checkpoint:
            raise ValueError("Checkpoint must contain a 'model' state_dict")
        log('Loading state_dict')
        self.model.load_state_dict(checkpoint['model'])
        del checkpoint
        log(f'Moving model to {self.device}')
        self.model = self.model.to(self.device)
        self.model.eval()
        log('Ready')

    def _predict_tile(self, image: np.ndarray) -> np.ndarray:
        x = image.astype(np.float32) / 255.0
        x = (x - IMAGENET_MEAN) / IMAGENET_STD
        x = torch.from_numpy(x.transpose(2, 0, 1)).unsqueeze(0).to(self.device)
        with torch.inference_mode():
            return torch.sigmoid(self.model(x)).squeeze().float().cpu().numpy()

    def probability_map(self, image: np.ndarray) -> np.ndarray:
        h, w = image.shape[:2]
        tile = self.cfg.tile_size
        step = max(1, round(tile * (1 - self.cfg.tile_overlap)))
        ys = list(range(0, max(1, h - tile + 1), step))
        xs = list(range(0, max(1, w - tile + 1), step))
        if ys[-1] != max(0, h - tile):
            ys.append(max(0, h - tile))
        if xs[-1] != max(0, w - tile):
            xs.append(max(0, w - tile))
        padded = cv2.copyMakeBorder(image, 0, max(0, tile - h), 0, max(0, tile - w), cv2.BORDER_REFLECT_101)
        prob = np.zeros(padded.shape[:2], np.float32)
        weight = np.zeros_like(prob)
        window = np.outer(np.hanning(tile), np.hanning(tile)).astype(np.float32)
        window = np.maximum(window, 1e-3)
        for y in ys:
            for x in xs:
                p = self._predict_tile(padded[y:y + tile, x:x + tile])
                prob[y:y + tile, x:x + tile] += p * window
                weight[y:y + tile, x:x + tile] += window
        return (prob / np.maximum(weight, 1e-6))[:h, :w]

    def predict(self, image_path: Path, gsd_mm_per_px: float | None = None) -> tuple[np.ndarray, np.ndarray, dict]:
        image = np.asarray(Image.open(image_path).convert('RGB'))
        probability = self.probability_map(image)
        mask = probability >= self.cfg.threshold
        mask = remove_small_objects(mask, min_size=self.cfg.min_component_px)
        measurement = measure_mask(mask, gsd_mm_per_px)
        return image, mask.astype(np.uint8), measurement


def measure_mask(mask: np.ndarray, gsd_mm_per_px: float | None) -> dict:
    skeleton = skeletonize(mask)
    # Count horizontal/vertical and diagonal edges once in the 8-neighbour graph.
    orth = (skeleton[:, 1:] & skeleton[:, :-1]).sum() + (skeleton[1:, :] & skeleton[:-1, :]).sum()
    diag = (skeleton[1:, 1:] & skeleton[:-1, :-1]).sum() + (skeleton[1:, :-1] & skeleton[:-1, 1:]).sum()
    length_px = float(orth + math.sqrt(2) * diag)
    distance = cv2.distanceTransform(mask.astype(np.uint8), cv2.DIST_L2, 5)
    widths = 2 * distance[skeleton]
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    boxes = [{'x': int(x), 'y': int(y), 'width': int(w), 'height': int(h), 'area_px': int(area)}
             for x, y, w, h, area in stats[1:]]
    result = {
        'crack_pixels': int(mask.sum()),
        'component_count': len(boxes),
        'boxes': sorted(boxes, key=lambda box: box['area_px'], reverse=True),
        'length_px': length_px,
        'width_px_mean': float(widths.mean()) if len(widths) else 0.0,
        'width_px_max': float(widths.max()) if len(widths) else 0.0,
    }
    if gsd_mm_per_px and gsd_mm_per_px > 0:
        result.update({
            'gsd_mm_per_px': float(gsd_mm_per_px),
            'length_mm': length_px * gsd_mm_per_px,
            'width_mm_mean': result['width_px_mean'] * gsd_mm_per_px,
            'width_mm_max': result['width_px_max'] * gsd_mm_per_px,
        })
    return result


def write_visuals(image: np.ndarray, mask: np.ndarray, measurement: dict, result_dir: Path) -> tuple[str, str]:
    result_dir.mkdir(parents=True, exist_ok=True)
    overlay = image.copy()
    overlay[mask.astype(bool)] = (255, 56, 56)
    blended = cv2.addWeighted(image, 0.62, overlay, 0.38, 0)
    for i, box in enumerate(measurement['boxes']):
        x, y, w, h = box['x'], box['y'], box['width'], box['height']
        cv2.rectangle(blended, (x, y), (x + w, y + h), (57, 220, 140), 2)
        cv2.putText(blended, f'Crack {i + 1}', (x, max(17, y - 6)), cv2.FONT_HERSHEY_SIMPLEX,
                    .55, (57, 220, 140), 2, cv2.LINE_AA)
    overlay_path, mask_path = result_dir / 'overlay.jpg', result_dir / 'mask.png'
    Image.fromarray(blended).save(overlay_path, quality=92)
    Image.fromarray(mask * 255).save(mask_path)
    return str(overlay_path), str(mask_path)
