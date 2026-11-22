"""Gain and dB conversions shared by the panel and the API."""

import math

# The MOTU's level resolution: it stores anything below 5e-7 as 0.
LEVEL_DECIMALS = 6


def limit_to_range(value: float, minimum: float, maximum: float) -> float:
    return max(min(value, maximum), minimum)


def gain_to_db(gain: int | float) -> float:
    """Linear gain to dB. 0 returns -inf."""
    gain = float(gain)
    if gain == 0:
        return -math.inf
    return 20 * math.log10(gain)


def db_to_gain(db: int | float) -> float:
    """dB to linear gain. -inf returns 0, beyond the float range inf."""
    try:
        return 10 ** (float(db) / 20)
    except OverflowError:
        return math.inf
