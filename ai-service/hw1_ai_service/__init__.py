"""hw1-ai-service: CM5-side AI companion for hardwareone.

Speaks the UART link (see ../../CM5_AI_SERVICE_PLAN.md), fetches voice from
the XIAO, runs STT + LLM on the CM5, and returns answers to the device's
display surfaces. Architecture: ../../ARCHITECTURE.md.
"""

__version__ = "0.1.0"
