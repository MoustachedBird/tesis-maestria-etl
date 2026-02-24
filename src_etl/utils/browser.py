from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service


def get_chrome_driver(headless: bool = True):
    """
    Configures and returns an optimized Chrome WebDriver instance.

    Args:
        headless (bool): If True, runs without the Graphical User Interface (UI).
    """
    print(f"🌐 Starting Chrome Browser (Headless={headless})...")

    chrome_options = Options()

    if headless:
        chrome_options.add_argument("--headless")

    # --- CRITICAL FLAGS FOR DOCKER/LINUX ---
    # Without these, Chrome crashes in server environments
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")

    # Optional: Disable notifications and popups
    prefs = {
        "profile.default_content_setting_values.notifications": 2,
        "download.default_directory": "/tmp",  # Optional
    }
    chrome_options.add_experimental_option("prefs", prefs)

    try:
        service = Service()
        # Selenium 4.x automatically finds the driver (selenium-manager)
        driver = webdriver.Chrome(service=service, options=chrome_options)
        return driver
    except Exception as e:
        print(f"❌ Fatal error starting Chrome: {e}")
        return None
