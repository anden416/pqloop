"""pqloop — VMAF-feedback picture quality optimization loop for ffmpeg.

Iteratively re-encodes a clip of the input, measures Netflix VMAF against a
lossless mezzanine reference, and hill-climbs encoder parameters ordered by
measured impact until returns diminish.
"""

__version__ = "0.1.0"
