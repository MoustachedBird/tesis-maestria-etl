"""Cómo consultar sitios ajenos sin que nos bloqueen, y cómo darse cuenta cuando lo hacen.

El agente lee Google, Bing, Maps y Facebook como lo haría una persona, sin
API. Eso funciona a volumen de muestra si se cumplen dos cosas:

- **Pausa por dominio.** Nunca dos peticiones seguidas al mismo sitio:
  `Pausador` espera `AGENTE_PAUSA_SEGUNDOS` (más un azar) desde la anterior.
  El tiempo de la corrida no es la restricción; un CAPTCHA sí.
- **Un bloqueo no es una lectura.** Una página de CAPTCHA o de "tráfico
  inusual" trae texto de sobra, así que la regla del mínimo de caracteres
  no la atrapa. `detectar_bloqueo` la reconoce, la pieza queda `ERROR` y el
  modelo no puede citarla. Es la misma regla que el muro de login de v2.

Si un motor bloquea varias veces seguidas, `Cortacircuitos` lo apaga por el
resto de la corrida: seguir insistiendo solo alarga el bloqueo. Cada consulta
saltada queda como pieza `ERROR`, así que el hueco se puede medir después
(`verificacion.consultas`).
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from src.shared.logging import get_stage_logger

logger = get_stage_logger(__name__, Path("./data/agente.log"))

_SENALES_DE_BLOQUEO: tuple[tuple[str, str], ...] = (
    ("captcha", r"\bcaptcha\b|recaptcha|hcaptcha"),
    ("tráfico inusual", r"unusual traffic|tr[aá]fico inusual|our systems have detected"),
    ("verificación humana", r"verify you are (a )?human|are you a robot|no soy un robot"),
    ("verificación humana", r"verifica que eres (un )?humano|confirma que eres humano"),
    ("desafío anti-bot", r"checking your browser|just a moment\.\.\.|attention required"),
    ("desafío anti-bot", r"bots use duckduckgo too|anomaly-modal"),
    ("acceso denegado", r"access denied|acceso denegado|request blocked|too many requests"),
)
"""Frases de una página de bloqueo, en inglés y español, con el motivo que se reporta."""

_URLS_DE_BLOQUEO: tuple[str, ...] = ("/sorry/", "google.com/sorry", "/captcha", "challenge")
"""Fragmentos de la URL a la que redirige un bloqueo (Google manda a /sorry/)."""

_MAX_TEXTO_BLOQUEO = 3000
"""Una página de bloqueo es corta. Por encima de esto, una mención de "captcha"
es parte del contenido (un sitio que habla de captchas, un pie de página con
reCAPTCHA) y no un bloqueo."""


def detectar_bloqueo(texto: str, url: str = "") -> str | None:
    """El motivo si la página es un bloqueo anti-bot; None si es contenido.

    Args:
        texto: el texto visible (o el HTML) de la página.
        url: la URL final, tras redirecciones.

    Returns:
        str | None: "captcha", "tráfico inusual"... o None.
    """
    u = url.lower()
    if any(f in u for f in _URLS_DE_BLOQUEO):
        return "redirigió a una página de verificación"
    t = (texto or "").lower()
    if len(t) > _MAX_TEXTO_BLOQUEO:
        # Las frases inequivocas cuentan a cualquier largo; "captcha" suelto no.
        for motivo, patron in _SENALES_DE_BLOQUEO[1:3]:
            if re.search(patron, t[:_MAX_TEXTO_BLOQUEO]):
                return motivo
        return None
    for motivo, patron in _SENALES_DE_BLOQUEO:
        if re.search(patron, t):
            return motivo
    return None


class Bloqueado(Exception):
    """Un sitio respondió con un bloqueo anti-bot en vez de contenido."""

    def __init__(self, sitio: str, motivo: str):
        super().__init__(f"{sitio} bloqueó la consulta ({motivo})")
        self.sitio = sitio
        self.motivo = motivo


class Pausador:
    """Espera entre dos peticiones al mismo dominio."""

    def __init__(
        self,
        pausa: float,
        *,
        azar: float = 0.5,
        reloj: Callable[[], float] = time.monotonic,
        dormir: Callable[[float], None] = time.sleep,
        aleatorio: Callable[[], float] = random.random,
    ):
        """
        Args:
            pausa: segundos mínimos entre dos peticiones al mismo dominio.
            azar: fracción de `pausa` que se suma al azar (0.5 = hasta 50% más),
                para que el ritmo no sea una firma.
            reloj, dormir, aleatorio: se inyectan en los tests.
        """
        self.pausa = pausa
        self.azar = azar
        self._reloj = reloj
        self._dormir = dormir
        self._aleatorio = aleatorio
        self._ultima: dict[str, float] = {}

    def esperar(self, url: str) -> None:
        """Duerme lo que falte para poder pedir `url` y registra la petición."""
        dominio = dominio_de(url)
        ahora = self._reloj()
        previa = self._ultima.get(dominio)
        if previa is not None and self.pausa > 0:
            objetivo = self.pausa * (1 + self.azar * self._aleatorio())
            falta = previa + objetivo - ahora
            if falta > 0:
                if falta >= 1:
                    logger.info(f"         ⏸  pausa {falta:.1f}s antes de volver a {dominio}")
                self._dormir(falta)
                ahora = self._reloj()
        self._ultima[dominio] = ahora


class Cortacircuitos:
    """Apaga un motor tras `max_bloqueos` bloqueos seguidos, por el resto de la corrida."""

    def __init__(self, max_bloqueos: int):
        self.max_bloqueos = max_bloqueos
        self._seguidos: dict[str, int] = {}

    def apagado(self, motor: str) -> bool:
        """Si el motor ya no se consulta en esta corrida."""
        return self.max_bloqueos > 0 and self._seguidos.get(motor, 0) >= self.max_bloqueos

    def registrar(self, motor: str, *, bloqueado: bool) -> None:
        """Cuenta un bloqueo; una respuesta buena reinicia la cuenta."""
        self._seguidos[motor] = self._seguidos.get(motor, 0) + 1 if bloqueado else 0


def dominio_de(url: str) -> str:
    """El dominio de una URL sin `www.`: la llave de la pausa."""
    return urlparse(url).netloc.lower().removeprefix("www.")
