"""
Structured logging system with Rich console output.

Provides three-tier logging:
1. JSON logs - Machine-readable for analysis
2. Rich console - Human-readable with colors
3. Error logs - Separate file for errors only
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import ClassVar

import structlog
from rich.console import Console
from rich.logging import RichHandler


class PipelineLogger:
    """
    Structured logger with Rich console output and run-specific directories.

    Creates logs in: logs/downloads/YYYY-MM-DD/run_TIMESTAMP/
    Maintains symlink: logs/downloads/latest -> most recent run

    Three log outputs:
    - Console: Rich formatted, human-readable
    - JSON file: Machine-readable, structured data
    - Error file: ERROR and CRITICAL only

    Example:
        >>> logger = PipelineLogger(
        ...     log_dir=Path("logs/downloads"),
        ...     run_id="550e8400-e29b-41d4-a716-446655440000",
        ...     job_name="denue_downloader",
        ... )
        >>> log = logger.get_logger()
        >>> log.info("download_started", url="https://example.com/data.zip")
        >>> log.error("download_failed", url="...", error="Connection timeout")
    """

    def __init__(
        self,
        log_dir: Path = Path("logs/downloads"),
        run_id: str | None = None,
        job_name: str = "etl",
        log_level: str = "INFO",
        enable_console: bool = True,
        enable_json: bool = True,
        enable_errors: bool = True,
    ):
        """
        Initialize structured logger.

        Args:
            log_dir: Base directory for logs
            run_id: Unique run identifier (for context binding)
            job_name: Job name (for context binding)
            log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            enable_console: Enable Rich console output
            enable_json: Enable JSON file output
            enable_errors: Enable separate error log
        """
        self.log_dir = Path(log_dir)
        self.run_id = run_id
        self.job_name = job_name
        self.log_level = log_level.upper()
        self.enable_console = enable_console
        self.enable_json = enable_json
        self.enable_errors = enable_errors

        # Create run-specific directory
        self.run_dir = self._create_run_directory()

        # Configure structlog
        self._configure_structlog()

        # Create logger instance
        self._logger = structlog.get_logger()

        # Bind run context
        if self.run_id:
            self._logger = self._logger.bind(run_id=self.run_id)
        if self.job_name:
            self._logger = self._logger.bind(job_name=self.job_name)

    def _create_run_directory(self) -> Path:
        """
        Create run-specific directory and update latest symlink.

        Directory structure:
        logs/downloads/
        ├── 2026-01-29/
        │   ├── run_20260129_103000/
        │   │   ├── app.log (JSON)
        │   │   └── errors.log
        │   └── run_20260129_140000/
        └── latest -> 2026-01-29/run_20260129_140000/

        Returns:
            Path to run directory
        """
        # Create date directory (YYYY-MM-DD)
        date_str = datetime.now().strftime("%Y-%m-%d")
        date_dir = self.log_dir / date_str
        date_dir.mkdir(parents=True, exist_ok=True)

        # El nombre lleva la hora porque es lo que se lee de un vistazo,
        # pero la unicidad NO se deja al reloj: dos corridas del mismo
        # segundo compartirian directorio y mezclarian sus lineas en un
        # mismo app.log, con el enlace `latest` apuntando al revoltijo. Si
        # el nombre ya existe se numera, que no depende de la resolucion
        # del reloj ni de cuanto tarde en arrancar una corrida.
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        nombre = f"run_{timestamp}"
        intento = 1
        while (date_dir / nombre).exists():
            intento += 1
            nombre = f"run_{timestamp}_{intento}"
        run_dir = date_dir / nombre
        run_dir.mkdir(parents=True)

        # Update 'latest' symlink
        latest_link = self.log_dir / "latest"
        if latest_link.exists() or latest_link.is_symlink():
            latest_link.unlink()

        # Create relative symlink for portability
        relative_path = Path(date_str) / nombre
        latest_link.symlink_to(relative_path)

        return run_dir

    #: Handlers que esta clase instalo en el root logger. Se guardan para
    #: poder retirarlos: sin esto, crear un segundo `PipelineLogger` en el
    #: mismo proceso --dos corridas, o un test tras otro-- apila handlers y
    #: cada linea se escribe tantas veces como corridas hubo.
    _instalados: ClassVar[list[logging.Handler]] = []

    def _configure_structlog(self) -> None:
        """Enchufa structlog al logging estandar, con un render por destino.

        El punto delicado de integrar structlog con `logging` es que el
        renderizado NO va en la cadena de procesadores: va en el *formatter*
        de cada handler. structlog deja el `event_dict` a medio camino
        (`wrap_for_formatter`) y cada handler decide como volverlo texto --la
        consola como linea legible, los archivos como JSON.

        Omitir ese formatter no falla: el handler escribe la repr del
        diccionario. Es lo que pasaba aqui, y por eso la consola mostraba
        `{'event': 'download_started', 'level': 'info', ...}` en vez de una
        linea, y `app.log` lo mismo en lugar de JSON por linea.

        `foreign_pre_chain` aplica los mismos procesadores a los registros
        que NO vienen de structlog --los de `logging.getLogger()` de las
        librerias-- para que el archivo salga homogeneo.
        """
        compartidos = [
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
        ]

        structlog.configure(
            processors=[
                *compartidos,
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )

        def formatter(render) -> structlog.stdlib.ProcessorFormatter:
            """Formatter de un handler, con el render que le corresponde."""
            return structlog.stdlib.ProcessorFormatter(
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    render,
                ],
                foreign_pre_chain=compartidos,
            )

        root = logging.getLogger()
        for viejo in self._instalados:
            root.removeHandler(viejo)
            viejo.close()
        self._instalados.clear()

        nuevos: list[logging.Handler] = []

        if self.enable_console:
            # RichHandler ya pone hora, nivel y origen, asi que el render
            # solo aporta el evento y sus campos. `markup=False` porque el
            # contenido trae corchetes (URLs, listas) que Rich interpretaria
            # como etiquetas y se comeria.
            consola = RichHandler(
                console=Console(stderr=True),
                rich_tracebacks=True,
                show_time=True,
                show_level=True,
                show_path=False,
                markup=False,
            )
            consola.setFormatter(formatter(structlog.dev.ConsoleRenderer(colors=False)))
            nuevos.append(consola)

        if self.enable_json:
            archivo = logging.FileHandler(self.json_log_path)
            archivo.setLevel(self.log_level)
            archivo.setFormatter(formatter(structlog.processors.JSONRenderer()))
            nuevos.append(archivo)

        if self.enable_errors:
            # Mismo contenido que app.log pero solo lo accionable: en una
            # corrida de 484 descargas, el archivo de errores es el que se
            # abre primero.
            errores = logging.FileHandler(self.error_log_path)
            errores.setLevel(logging.ERROR)
            errores.setFormatter(formatter(structlog.processors.JSONRenderer()))
            nuevos.append(errores)

        for handler in nuevos:
            root.addHandler(handler)
        self._instalados.extend(nuevos)
        root.setLevel(self.log_level)

    def get_logger(self) -> structlog.BoundLogger:
        """
        Get configured logger instance.

        Returns:
            Bound logger with run context

        Example:
            >>> logger_manager = PipelineLogger(run_id="abc123")
            >>> log = logger_manager.get_logger()
            >>> log.info("task_started", task_id="xyz789")
        """
        return self._logger

    def bind(self, **kwargs) -> structlog.BoundLogger:
        """
        Bind additional context to logger.

        Args:
            **kwargs: Context key-value pairs

        Returns:
            Logger with bound context

        Example:
            >>> log = logger_manager.bind(task_id="task123", attempt=2)
            >>> log.info("download_started")  # Includes task_id and attempt
        """
        return self._logger.bind(**kwargs)

    def unbind(self, *keys) -> structlog.BoundLogger:
        """
        Remove context from logger.

        Args:
            *keys: Context keys to remove

        Returns:
            Logger without specified context

        Example:
            >>> log = logger_manager.unbind("task_id")
        """
        return self._logger.unbind(*keys)

    @property
    def log_directory(self) -> Path:
        """Get current run log directory."""
        return self.run_dir

    @property
    def json_log_path(self) -> Path:
        """Get path to JSON log file."""
        return self.run_dir / "app.log"

    @property
    def error_log_path(self) -> Path:
        """Get path to error log file."""
        return self.run_dir / "errors.log"

    @classmethod
    def create_for_run(
        cls,
        run_id: str,
        job_name: str,
        log_dir: Path = Path("logs/downloads"),
        log_level: str = "INFO",
    ) -> "PipelineLogger":
        """
        Factory method to create logger for a specific job run.

        Args:
            run_id: Unique run identifier
            job_name: Job name
            log_dir: Base log directory
            log_level: Logging level

        Returns:
            Configured PipelineLogger instance

        Example:
            >>> logger = PipelineLogger.create_for_run(
            ...     run_id="550e8400-e29b-41d4-a716-446655440000",
            ...     job_name="denue_downloader",
            ... )
        """
        return cls(
            log_dir=log_dir,
            run_id=run_id,
            job_name=job_name,
            log_level=log_level,
        )

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"PipelineLogger("
            f"job={self.job_name}, "
            f"run_id={self.run_id}, "
            f"level={self.log_level}, "
            f"dir={self.run_dir})"
        )


def get_logger(name: str | None = None) -> structlog.BoundLogger:
    """
    Get a structlog logger instance.

    Convenience function for getting a basic logger without
    PipelineLogger setup.

    Args:
        name: Logger name (optional)

    Returns:
        Bound logger instance

    Example:
        >>> log = get_logger(__name__)
        >>> log.info("operation_started")
    """
    if name:
        return structlog.get_logger(name)
    return structlog.get_logger()


def get_stage_logger(name: str, log_file: Path) -> logging.Logger:
    """
    Logger estandar de stage: archivo dedicado + consola, sin side effects.

    Punto UNICO de configuracion de logging para los stages del transform
    (normalize-dates, h3-cells, ingest, etc.). Reemplaza los
    `logging.basicConfig()` a nivel de modulo que existian en varios
    stages: aquellos configuraban el ROOT logger de todo el proceso al
    importar (gana el primero importado), pisandose entre si. Este helper
    configura solo el logger nombrado, con `propagate=False` para no
    duplicar lineas si el root tambien tiene handlers.

    Idempotente: si el logger ya tiene handlers, se retorna tal cual
    (seguro llamarlo en cada import del modulo).

    Args:
        name: Nombre del logger (usar __name__ del modulo del stage).
        log_file: Archivo de log dedicado del stage (ej. data/etl_h3.log).

    Returns:
        logging.Logger configurado con FileHandler + StreamHandler(stdout).

    Example:
        >>> logger = get_stage_logger(__name__, Path("./data/etl_process.log"))
        >>> logger.info("stage started")
    """
    import sys as _sys

    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    log_file.parent.mkdir(parents=True, exist_ok=True)

    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(_sys.stdout)
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
