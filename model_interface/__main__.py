"""model-interface --config model-interface.toml [--host 127.0.0.1] [--port 8080]"""
import argparse

import uvicorn

from .config import load
from .server import create_app


def main():
    parser = argparse.ArgumentParser(prog="model-interface", description=__doc__)
    parser.add_argument("--config", default="model-interface.toml")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    uvicorn.run(create_app(load(args.config)), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
