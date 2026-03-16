#!/usr/bin/env python3
"""
Hyperfocus Runner
=================
Quick start script for development.

Usage:
    python run.py          # Start server
    python run.py --port 8080
    python run.py --reload # Auto-reload on changes
"""

import argparse
import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))


def main():
    parser = argparse.ArgumentParser(description="Run Hyperfocus server")
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind to (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to bind to (default: 8080)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload for development",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of workers (default: 1, use 1 for WebSocket support)",
    )
    args = parser.parse_args()

    # Check for .env file
    env_file = project_root / ".env"
    env_example = project_root / ".env.example"

    if not env_file.exists() and env_example.exists():
        print("⚠️  No .env file found. Creating from .env.example...")
        import shutil
        shutil.copy(env_example, env_file)
        print("   Created .env - please review and update settings!")

    # Import uvicorn here to avoid issues if not installed
    try:
        import uvicorn
    except ImportError:
        print("❌ uvicorn not installed. Run: pip install uvicorn[standard]")
        sys.exit(1)

    print(f"""
╔═══════════════════════════════════════════════════════════════╗
║                                                               ║
║   ██╗  ██╗██╗   ██╗██████╗ ███████╗██████╗                   ║
║   ██║  ██║╚██╗ ██╔╝██╔══██╗██╔════╝██╔══██╗                  ║
║   ███████║ ╚████╔╝ ██████╔╝█████╗  ██████╔╝                  ║
║   ██╔══██║  ╚██╔╝  ██╔═══╝ ██╔══╝  ██╔══██╗                  ║
║   ██║  ██║   ██║   ██║     ███████╗██║  ██║                  ║
║   ╚═╝  ╚═╝   ╚═╝   ╚═╝     ╚══════╝╚═╝  ╚═╝   FOCUS          ║
║                                                               ║
╚═══════════════════════════════════════════════════════════════╝

   Starting server on http://{args.host}:{args.port}
   API docs: http://localhost:{args.port}/api/docs
   
   Press Ctrl+C to stop
""")

    uvicorn.run(
        "backend.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=args.workers,
        log_level="info",
    )


if __name__ == "__main__":
    main()
