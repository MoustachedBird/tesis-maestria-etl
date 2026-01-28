import re
import unicodedata

def clean_filename(text: str) -> str:
    """
    Converts a raw string into a filesystem-safe filename.
    Example: 'Comercio al por Menor (2024).zip' -> 'comercio_al_por_menor_2024.zip'
    """
    if not text:
        return "unnamed_file"
        
    # 1. Convert to lowercase
    text = text.lower()
    
    # 2. Replace spaces and dots with underscores
    text = text.replace(' ', '_').replace('.', '_')
    
    # 3. Unicode Normalization (Decompose characters)
    # Example: 'ó' becomes 'o' + '´' (NFD form)
    nfd_form = unicodedata.normalize('NFD', text)
    
    # 4. Filter out non-spacing mark characters (accents/Mn)
    text_without_accents = "".join([c for c in nfd_form if unicodedata.category(c) != 'Mn'])
    
    # 5. Remove any character that is not alphanumeric, underscore, or hyphen
    clean_text = re.sub(r'[^a-z0-9_-]', '', text_without_accents)
    
    # 6. Remove repeated underscores (e.g. ___)
    clean_text = re.sub(r'__+', '_', clean_text)
    
    # 7. Strip leading or trailing underscores
    return clean_text.strip('_')