"""Alert output sinks package."""

from dxlink_scanner.sinks.stdout_sink import StdoutSink, _alert_to_dict, _json_default
from dxlink_scanner.sinks.webhook_sink import WebhookSink

__all__ = ["StdoutSink", "WebhookSink", "_alert_to_dict", "_json_default"]
