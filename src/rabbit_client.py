"""Publicador de notificaciones de email vía RabbitMQ (misma estructura que EmailMessageDTO de Java)."""
from __future__ import annotations

import json
import logging
from typing import List

import pika

log = logging.getLogger(__name__)

_EXCHANGE = "notifications-exchange"
_ROUTING_KEY = "notification.it"


def send_email_notification(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    vhost: str,
    destinatarios: List[str],
    modelo_correo: int,
    cuenta_correo: int,
    auth: str,
    cc: str,
    variables: List[str],
) -> None:
    payload = {
        "destinatarios": ",".join(destinatarios),
        "modeloCorreo": modelo_correo,
        "cuentaCorreo": cuenta_correo,
        "cc": cc,
        "auth": auth,
        "variables": variables,
    }
    try:
        credentials = pika.PlainCredentials(user, password)
        params = pika.ConnectionParameters(
            host=host,
            port=port,
            virtual_host=vhost,
            credentials=credentials,
            connection_attempts=2,
            retry_delay=2,
            socket_timeout=10,
        )
        connection = pika.BlockingConnection(params)
        channel = connection.channel()
        body = json.dumps(payload)
        log.debug("RabbitMQ payload: exchange=%s routing_key=%s body=%s", _EXCHANGE, _ROUTING_KEY, body)
        channel.basic_publish(
            exchange=_EXCHANGE,
            routing_key=_ROUTING_KEY,
            body=body,
            properties=pika.BasicProperties(
                content_type="application/json",
                delivery_mode=2,  # persistent
            ),
        )
        connection.close()
        log.info("Notificación de error enviada a RabbitMQ para: %s", ", ".join(destinatarios))
    except Exception as exc:
        log.warning("No se pudo enviar notificación de email vía RabbitMQ: %s", exc)
