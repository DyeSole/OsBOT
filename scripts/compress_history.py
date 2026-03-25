from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.settings import BASE_DIR, load_settings
from app.infra.storage import ChatHistoryStore, CompressionStore
from app.services.compression_service import CompressionService


def main() -> int:
    parser = argparse.ArgumentParser(description="Compress active chat history for a channel.")
    parser.add_argument("channel_id", type=int, help="Discord channel ID")
    args = parser.parse_args()

    settings = load_settings()
    history_store = ChatHistoryStore(BASE_DIR / "data" / "chat_history")
    compression_store = CompressionStore(BASE_DIR / "data" / "memory")
    service = CompressionService(
        settings=settings,
        history_store=history_store,
        compression_store=compression_store,
    )

    segment = service.compress_history(channel_id=args.channel_id)
    if segment is None:
        print(f"no active messages to compress for channel {args.channel_id}")
        return 0

    print("compression complete")
    print(f"range: {segment['start_time']} -> {segment['end_time']}")
    print(f"keywords: {segment['keywords']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
