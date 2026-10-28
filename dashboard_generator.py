import os
import webbrowser


def _dashboard_html(dashboard_template_path: str = "dashboard.html", api_base: str = "") -> str:
    """Beolvassa a különálló dashboard.html sablont és behelyettesíti az API címet."""
    if not os.path.exists(dashboard_template_path):
        raise FileNotFoundError(f"A sablon fájl nem található: {dashboard_template_path}")
    
    with open(dashboard_template_path, "r", encoding="utf-8") as f:
        template_content = f.read()
        
    return template_content.replace("__API__", api_base)


def generate_dashboard(
    json_path="database.json",
    output_html="dashboard.html",
    open_browser=True,
    dashboard_url="http://127.0.0.1:8765/"
):
    """Létrehozza a végleges dashboard.html fájlt és elindítja a böngészőt."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    target_json = os.path.join(base_dir, json_path) if not os.path.isabs(json_path) else json_path
    
    if not os.path.exists(target_json):
        print(f"[DASHBOARD] A(z) '{target_json}' adatbázis fájl nem található.")
        return

    target_html = os.path.join(base_dir, output_html) if not os.path.isabs(output_html) else output_html
    
    try:
        # Itt szándékosan üres stringet ("") adunk át api_base-ként, hogy a JS-ben ne legyen dupla perjel!
        content = _dashboard_html(target_html, api_base="")
        with open(target_html, "w", encoding="utf-8") as file:
            file.write(content)
        print(f"[DASHBOARD] Dashboard elindítva / frissítve: {target_html}")
    except Exception as e:
        print(f"[DASHBOARD ERROR] Hiba a dashboard generálásakor: {e}")

    # Itt viszont a teljes dashboard_url-t használjuk a böngésző megnyitásához!
    if open_browser:
        webbrowser.open(dashboard_url)


def reset_dashboard(json_path="database.json", output_html="dashboard.html", open_browser=False):
    generate_dashboard(json_path, output_html, open_browser)


if __name__ == "__main__":
    generate_dashboard()