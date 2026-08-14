"""Local stand-in for the SMS webhook.

Outbound delivery has to succeed or nothing works: _persist_and_send_bot_reply
sends before it commits, so a failed POST rolls the reply text back, and
build_chat_history only admits bot turns with status='sent'. A rejected webhook
would quietly turn the conversation run into a second isolated run.

Answers 200 to everything and appends each payload to a JSONL file.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

LOG_PATH = Path("/tmp/memtest_sms_sink.jsonl")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {"_unparsed": raw.decode("utf-8", "replace")}

        with LOG_PATH.open("a") as handle:
            handle.write(
                json.dumps({"received_at": datetime.now(UTC).isoformat(), **payload}) + "\n"
            )

        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Quiet: the JSONL file is the record."""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9099)
    args = parser.parse_args()

    LOG_PATH.touch(exist_ok=True)
    print(f"SMS sink on :{args.port}, logging to {LOG_PATH}", flush=True)
    HTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
