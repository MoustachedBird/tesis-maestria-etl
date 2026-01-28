import requests

def download_file_streaming(url, destination):
    """Descarga eficiente por streaming con manejo de errores básico."""
    try:
        with requests.get(url, stream=True, timeout=60) as r:
            if r.status_code == 404:
                return "NOK", "404 - Not Found"
            r.raise_for_status()
            with open(destination, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        return "OK", "Downloaded successfully"
    except Exception as e:
        return "NOK", str(e)[:100]