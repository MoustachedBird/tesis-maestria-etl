import typer
from scripts.sources.inegi import app as inegi_app

app = typer.Typer(help="Runner de Scripts ETL para la Tesis")

# Agregamos los sub-comandos de cada fuente
app.add_typer(inegi_app, name="inegi", help="Descarga y proceso de datos del INEGI")

if __name__ == "__main__":
    app()
