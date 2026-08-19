"""hw1-ai-service: CM5-side AI companion for hardwareone.

Speaks the UART link, fetches voice from the ESP32, runs STT + LLM on the CM5,
and returns answers to the device's display surfaces.
Architecture: ../../docs/ARCHITECTURE.md.
"""

__version__ = "0.1.0"
