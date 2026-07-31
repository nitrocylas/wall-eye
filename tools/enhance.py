"""Recover a usable image from a camera whose IR-cut filter is missing.

Without an IR-cut filter, infrared light reaches the sensor that the scene's
colours do not account for. That shows up as a low-contrast veil with a colour
cast, and no sensor setting removes it - the extra light is real, it just does
not belong to the visible image. What can be undone afterwards is its effect:
the veil sets a floor no pixel falls below, so subtracting that floor per
channel restores both contrast and neutral colour in one step.

Order matters, and not in the obvious direction. Denoising first destroys the
image: under the veil the real detail is as low-amplitude as the noise, so a
denoiser cannot tell them apart and smears both away. Contrast is restored
first, and grain is dealt with by downscaling afterwards, which averages noise
while keeping edges.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

# Percentiles used to find the veil floor and the usable ceiling. The floor is
# not the true minimum: a few dead-black pixels would peg it and defeat the
# subtraction.
DEFAULT_FLOOR_PERCENTILE = 1.0
DEFAULT_CEILING_PERCENTILE = 99.5
DEFAULT_CLAHE_CLIP = 1.8
DEFAULT_CLAHE_TILES = 8
# Off by default: downscaling to the working width removes grain more cleanly
# than any filter, and NLM denoising costs far more time than it earns here.
DEFAULT_DENOISE_STRENGTH = 0
DEFAULT_SHARPEN_AMOUNT = 0.4
DEFAULT_SHARPEN_RADIUS = 2.0

# 1.0 keeps the full (very wide) field of view; 0.65 trims the fisheye edges
# while still leaving more pixels than the output width needs.
DEFAULT_KEEP_FRACTION = 1.0
DEFAULT_BARREL_K1 = 0.0        # negative undoes barrel distortion
DEFAULT_BRIGHTNESS_GAIN = 1.0  # multiplier applied after the veil is removed

MAX_CHANNEL_VALUE = 255.0
MIN_SPAN = 1.0  # guards against divide-by-zero on a flat channel


@dataclass(frozen=True)
class EnhanceSettings:
    """Tunables for the recovery pipeline."""

    floor_percentile: float = DEFAULT_FLOOR_PERCENTILE
    ceiling_percentile: float = DEFAULT_CEILING_PERCENTILE
    keep_fraction: float = DEFAULT_KEEP_FRACTION
    barrel_k1: float = DEFAULT_BARREL_K1
    brightness_gain: float = DEFAULT_BRIGHTNESS_GAIN
    clahe_clip: float = DEFAULT_CLAHE_CLIP
    clahe_tiles: int = DEFAULT_CLAHE_TILES
    denoise_strength: int = DEFAULT_DENOISE_STRENGTH
    sharpen_amount: float = DEFAULT_SHARPEN_AMOUNT
    sharpen_radius: float = DEFAULT_SHARPEN_RADIUS


def remove_veil(image: np.ndarray, floor_percentile: float, ceiling_percentile: float) -> np.ndarray:
    """Subtract each channel's haze floor and rescale to full range.

    Doing this per channel neutralises the colour cast as a side effect: the
    cast is an unequal floor across channels, so removing each one equalises them.
    """
    result = image.astype(np.float32)
    for channel in range(result.shape[2]):
        values = result[:, :, channel]
        floor = float(np.percentile(values, floor_percentile))
        ceiling = float(np.percentile(values, ceiling_percentile))
        span = max(ceiling - floor, MIN_SPAN)
        result[:, :, channel] = np.clip((values - floor) * (MAX_CHANNEL_VALUE / span),
                                        0, MAX_CHANNEL_VALUE)
    return result.astype(np.uint8)


def denoise(image: np.ndarray, strength: int) -> np.ndarray:
    """Reduce sensor grain. A strength of 0 disables it."""
    if strength <= 0:
        return image
    return cv2.fastNlMeansDenoisingColored(image, None, strength, strength, 7, 21)


def local_contrast(image: np.ndarray, clip: float, tiles: int) -> np.ndarray:
    """Apply CLAHE to lightness only, leaving hue and saturation untouched."""
    if clip <= 0:
        return image
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    clahe = cv2.createCLAHE(clipLimit=clip, tileGridSize=(tiles, tiles))
    lab[:, :, 0] = clahe.apply(lab[:, :, 0])
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def sharpen(image: np.ndarray, amount: float, radius: float) -> np.ndarray:
    """Unsharp mask, countering the softness IR defocus introduces."""
    if amount <= 0:
        return image
    blurred = cv2.GaussianBlur(image, (0, 0), radius)
    return cv2.addWeighted(image, 1.0 + amount, blurred, -amount, 0)


def center_crop(image: np.ndarray, keep_fraction: float) -> np.ndarray:
    """Keep the middle `keep_fraction` of each axis, narrowing the field of view.

    Usually free: sensors deliver far more pixels than the output width needs,
    so cropping before the downscale trims a too-wide lens without losing
    output resolution.
    """
    if not 0 < keep_fraction < 1:
        return image
    height, width = image.shape[:2]
    crop_width = max(1, int(width * keep_fraction))
    crop_height = max(1, int(height * keep_fraction))
    left = (width - crop_width) // 2
    top = (height - crop_height) // 2
    return image[top:top + crop_height, left:left + crop_width]


def correct_barrel(image: np.ndarray, strength: float) -> np.ndarray:
    """Straighten fisheye bulge. Negative k1 undoes barrel distortion."""
    if strength == 0:
        return image
    height, width = image.shape[:2]
    focal = float(width)  # adequate approximation for a single-parameter fit
    camera_matrix = np.array([[focal, 0, width / 2.0],
                              [0, focal, height / 2.0],
                              [0, 0, 1]], dtype=np.float32)
    coefficients = np.array([strength, 0.0, 0.0, 0.0], dtype=np.float32)
    return cv2.undistort(image, camera_matrix, coefficients)


def fit_width(image: np.ndarray, width: int) -> np.ndarray:
    """Downscale to `width`, preserving aspect. INTER_AREA averages away grain."""
    if width <= 0 or image.shape[1] <= width:
        return image
    height = max(1, round(width * image.shape[0] / image.shape[1]))
    return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)


def enhance(image: np.ndarray, settings: EnhanceSettings | None = None) -> np.ndarray:
    """Run the full recovery pipeline over a BGR image, at its input resolution."""
    if image is None or image.size == 0:
        raise ValueError("empty image")
    config = settings or EnhanceSettings()
    # Crop first: the veil floor should be measured from the region actually
    # kept, not from vignetted corners that are about to be thrown away.
    result = center_crop(image, config.keep_fraction)
    result = correct_barrel(result, config.barrel_k1)
    result = remove_veil(result, config.floor_percentile, config.ceiling_percentile)
    if config.brightness_gain != 1.0:
        result = np.clip(result.astype(np.float32) * config.brightness_gain,
                         0, MAX_CHANNEL_VALUE).astype(np.uint8)
    result = local_contrast(result, config.clahe_clip, config.clahe_tiles)
    result = sharpen(result, config.sharpen_amount, config.sharpen_radius)
    # Last, and normally disabled - see the module docstring on ordering.
    return denoise(result, config.denoise_strength)


def process(image: np.ndarray, width: int, settings: EnhanceSettings | None = None) -> np.ndarray:
    """Enhance at full resolution, then downscale.

    Enhancing first means the veil floor is estimated from every pixel; the
    downscale afterwards then averages the amplified grain instead of baking it in.
    """
    return fit_width(enhance(image, settings), width)


def contrast_of(image: np.ndarray) -> float:
    """Standard deviation of luminance - the headline 'can I see anything' number."""
    return float(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).std())


def colour_cast_of(image: np.ndarray) -> float:
    """Spread between channel means; 0 is neutral."""
    means = [float(image[:, :, c].mean()) for c in range(image.shape[2])]
    return max(means) - min(means)
