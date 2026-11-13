"""Configuración de logging a stdout (Docker recoge el log driver)."""
import logging
import sys
from typing import Optional


def setup_logging(level: str, extra_handler: Optional[logging.Handler] = None) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())

    handler = logging.StreamHandler(stream=sys.stdout)
    formatter = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03dZ %(levelname)-8s %(name)s - %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    handler.setFormatter(formatter)

    root.handlers.clear()
    root.addHandler(handler)

    if extra_handler is not None:
        root.addHandler(extra_handler)

    # Librerías internas del servidor web: solo queremos sus warnings/errores en stdout,
    # no un log de acceso por cada petición del panel (polling cada pocos segundos).
    logging.getLogger("waitress").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    # Al recibir SIGTERM, el trigger de Waitress se cierra antes de que los hilos worker
    # terminen sus requests en curso, produciendo un OSError EBADF inofensivo. Se suprime.
    logging.getLogger("waitress.trigger").setLevel(logging.CRITICAL)
