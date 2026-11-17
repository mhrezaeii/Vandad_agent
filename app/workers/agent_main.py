"""Entrypoint for the dedicated agent-task worker."""
import asyncio
import logging

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.workers.agent_worker import AgentTaskWorker


def main() -> None:
    settings = get_settings()
    setup_logging(settings)
    logging.getLogger("app.agent_worker").info("starting agent task runtime")
    asyncio.run(AgentTaskWorker().run())


if __name__ == "__main__":
    main()
