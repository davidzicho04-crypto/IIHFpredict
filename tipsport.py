from curl_cffi import requests as cffi_requests
import json
import time
import os
import shin
from datetime import datetime
from collections import defaultdict

COOKIE_SOUBOR = "cookie.txt"
# Změněno na index.html pro potřeby GitHub Pages
VYSTUP_HTML = "index.html"

# --- ČÁST 1: STAHOVÁNÍ DAT Z TIPSPORTU ---

def nacti_cookie():
    # 1. Nejprve zkusí načíst cookie z prostředí (GitHub Secrets)
    cookie_env = os.getenv("TIPSPORT_COOKIE")
    if cookie_env:
        return cookie_env.strip()
        
    # 2. Pokud běžíš lokálně na Macu, zkusí najít soubor cookie.txt
    if os.path.exists(COOKIE_SOUBOR):
        with open(COOKIE_SOUBOR, "r") as f:
            return f.read().strip()
            
    # Pokud cookie není vůbec, nevadí, kurzy zkusíme stáhnout veřejně
    return None

def vytvor_session(cookie):
    session = cffi_requests.Session(impersonate="chrome")
    if cookie:
        for polozka in cookie.split("; "):
            if "=" in polozka:
                nazev, _, hodnota = polozka.partition("=")
                session.cookies.set(nazev.strip(), hodnota.strip(), domain=".tipsport.cz")
    return session

def stahni_seznam_zapasu(session, headers):
    url = "https://www.tipsport.cz/rest/offer/v2/offer?limit=75"
    json_zaklad = {
        "results": False, "highlightAnyTime": False, "limit": 75,
        "order": "DATESTART", "type": "SUPERGROUP", "id": 941,
        "matchViewFilters": [], "withLive": True,
    }
    r = session.post(url, headers=headers, json=json_zaklad, timeout=15)
    if r.status_code != 200:
        return []
    data = r.json()
    try:
        zapasy = data["offerSuperSports"][0]["tabs"][0]["offerCompetitionAnnuals"][0]["matches"]
        return [z for z in zapasy if z.get("matchOfferType") == "ODD" and not z.get("race")]
    except (KeyError, IndexError):
        return []

def stahni_detail_zapasu(session, headers, zapas_id):
    url = f"https://www.tipsport.cz/rest/offer/v3/matches/{zapas_id}?fromResults=false&ticketBuilderId=1"
    r = session.get(url, headers=headers, timeout=15)
    if r.status_code != 200:
        return None
    return r.json()

def parsuj_detail(detail):
    vysledek = {"presne_skore": []}
    if not detail or "match" not in detail:
        return vysledek

    zapas = detail["match"]
    for tabulka in zapas.get("eventTables", []):
        tid = tabulka.get("id", "")
        if "EXACT_RESULTgp1-1" in tid:
            for box in tabulka.get("boxes", []):
                for cell in box.get("cells", []):
                    if cell.get("active") and ":" in str(cell.get("name", "")):
                        vysledek["presne_skore"].append({
                            "skore": cell["name"],
                            "kurz": cell["odd"]
                        })

    vysledek["presne_skore"].sort(key=lambda x: x["kurz"])
    return vysledek


# --- ČÁST 2: VÝPOČET EV A SHINOVA METODA ---

def odstran_marzi(kurzy):
    if not kurzy: return []
    
    smysluplne_kurzy = [k for k in kurzy if k["kurz"] < 200.0]
    if len(smysluplne_kurzy) < 2: 
        return []

    ciste_kurzy = [k["kurz"] for k in smysluplne_kurzy]
    
    try:
        shin_vysledek = shin.calculate_implied_probabilities(ciste_kurzy)
        if isinstance(shin_vysledek, dict):
            fair_probs = shin_vysledek['true_probabilities']
        else:
            fair_probs = shin_vysledek
    except Exception as e:
        print(f"    ⚠️ Shinova metoda selhala u tohoto zápasu: {e}")
        return []
        
    vysledek = []
    for i, k in enumerate(smysluplne_kurzy):
        fair_prob = fair_probs[i]
        fair_kurz = 1 / fair_prob if fair_prob > 0 else 0
        vysledek.append({
            "skore": k["skore"],
            "kurz_ts": k["kurz"],
            "fair_kurz": round(fair_kurz, 2),
            "pravdepodobnost": fair_prob
        })
        
    return vysledek

def spocitaj_ocekavane_body(tip_skore, vsechny_vysledky):
    try:
        tip_domaci, tip_hoste = map(int, tip_skore.split(":"))
    except ValueError:
        return 0.0

    tip_vitez = 1 if tip_domaci > tip_hoste else (2 if tip_hoste > tip_domaci else 0)
    tip_rozdil = tip_domaci - tip_hoste
    tip_goly = tip_domaci + tip_hoste
    ocekavane_body = 0.0

    for vysledek in vsechny_vysledky:
        try:
            real_domaci, real_hoste = map(int, vysledek["skore"].split(":"))
        except ValueError:
            continue
            
        real_vitez = 1 if real_domaci > real_hoste else (2 if real_hoste > real_domaci else 0)
        real_rozdil = real_domaci - real_hoste
        real_goly = real_domaci + real_hoste
        pravdepodobnost = vysledek["pravdepodobnost"]

        body = 0
        if tip_domaci == real_domaci and tip_hoste == real_hoste:
            body = 10
        elif (tip_vitez == 0 and real_vitez == 0):
            body = 6
        elif tip_vitez == real_vitez and (tip_rozdil == real_rozdil or tip_goly == real_goly):
            body = 6
        elif tip_vitez == real_vitez:
            body = 4
        elif tip_goly == real_goly:
            body = 2

        ocekavane_body += body * pravdepodobnost

    return round(ocekavane_body, 2)

def vygeneruj_html(vstup="data.json", vystup=VYSTUP_HTML):
    with open(vstup, "r", encoding="utf-8") as f:
        data = json.load(f)

    zapas_podle_dne = defaultdict(list)
    for zapas in data:
        try:
            dt = datetime.fromisoformat(zapas["datum"])
            zapas["dt_obj"] = dt
            zapas["cas_str"] = dt.strftime("%H:%M")
            den_klic = dt.strftime("%d. %m. %Y")
        except:
            zapas["dt_obj"] = datetime.max
            zapas["cas_str"] = "?:?"
            den_klic = "Neznámé datum"
        zapas_podle_dne[den_klic].append(zapas)

    html = """
    <!DOCTYPE html>
    <html lang="cs">
    <head>
        <meta charset="UTF-8">
        <title>Tipsport Tipovačka - SHIN MODEL</title>
        <style>
            body { font-family: -apple-system, system-ui, sans-serif; background: #f0f2f5; padding: 20px; color: #333; }
            .container { max-width: 950px; margin: 0 auto; }
            .date-header { background: #1a73e8; color: white; padding: 10px 20px; border-radius: 8px; margin: 30px 0 15px; font-weight: bold; font-size: 1.1em;}
            .match-card { background: white; border-radius: 12px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
            .match-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; border-bottom: 1px solid #eee; padding-bottom: 10px; }
            .match-title { font-size: 1.2em; font-weight: bold; }
            .match-odds { font-size: 0.9em; color: #666; background: #f8f9fa; padding: 6px 12px; border-radius: 6px;}
            table { width: 100%; border-collapse: collapse; margin-top: 10px; }
            th { text-align: left; font-size: 0.85em; color: #777; padding: 10px 8px; border-bottom: 2px solid #eee; text-transform: uppercase;}
            td { padding: 12px 8px; border-bottom: 1px solid #eee; }
            .score-cell { font-weight: bold; font-size: 1.1em;}
            .fair-prob { color: #888; font-size: 0.9em;}
            .ev-points { font-weight: bold; color: #155724; background: #d4edda; padding: 4px 8px; border-radius: 6px; font-size: 1.1em;}
            .hidden-row { display: none; }
            .show-more-btn { 
                display: block; width: 100%; padding: 12px; margin-top: 15px;
                background: #f8f9fa; border: 1px solid #ddd; border-radius: 8px;
                cursor: pointer; font-weight: bold; color: #555; transition: 0.2s;
            }
            .show-more-btn:hover { background: #e2e6ea; }
            .time { color: #1a73e8; margin-right: 8px; }
            .shin-badge { background: #6f42c1; color: white; padding: 2px 6px; border-radius: 4px; font-size: 0.8em; vertical-align: middle; margin-left: 10px; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1 style="text-align:center">🏒 Tipovačka MS 2026 - Očekávané body <span class="shin-badge">SHIN MODEL</span></h1>
    """

    serazene_dny = sorted(zapas_podle_dne.keys(), key=lambda d: zapas_podle_dne[d][0].get("dt_obj", datetime.max))
    
    for den in serazene_dny:
        zapasy_v_dnu = sorted(zapas_podle_dne[den], key=lambda x: x.get("dt_obj", datetime.max))
        validni_zapasy = []
        
        for zapas in zapasy_v_dnu:
            ocistene = odstran_marzi(zapas.get("presne_skore", []))
            if ocistene:
                pro_zobrazeni = []
                for tip in ocistene:
                    ev_body = spocitaj_ocekavane_body(tip["skore"], ocistene)
                    pro_zobrazeni.append({
                        "skore": tip["skore"],
                        "fair_kurz": tip["fair_kurz"],
                        "pravdepodobnost": round(tip["pravdepodobnost"] * 100, 2),
                        "ev": ev_body
                    })
                pro_zobrazeni.sort(key=lambda x: x["ev"], reverse=True)
                validni_zapasy.append((zapas, pro_zobrazeni))
        
        if not validni_zapasy:
            continue

        html += f"<div class='date-header'>🗓️ {den}</div>"
        
        for idx, (zapas, vysledky_ev) in enumerate(validni_zapasy):
            match_id = f"match_{den.replace(' ', '')}_{idx}"
            
            html += f"""
            <div class="match-card">
                <div class="match-header">
                    <div class="match-title"><span class="time">{zapas['cas_str']}</span>{zapas['nazev']}</div>
                    <div class="match-odds">1: <b>{zapas['kurz_1']}</b> | X: <b>{zapas['kurz_x']}</b> | 2: <b>{zapas['kurz_2']}</b></div>
                </div>
                <table id="table_{match_id}">
                    <tr>
                        <th>Tip Skóre</th>
                        <th>EV (Očekávané body)</th>
                        <th>Šance (Shin)</th>
                        <th>Fair Kurz</th>
                    </tr>
            """
            
            for i, s in enumerate(vysledky_ev):
                row_class = "hidden-row" if i >= 5 else ""
                html += f"""
                    <tr class="{row_class}">
                        <td class="score-cell">{s['skore']}</td>
                        <td><span class="ev-points">{s['ev']} bodů</span></td>
                        <td class="fair-prob">{s['pravdepodobnost']} %</td>
                        <td style="color:#aaa">{s['fair_kurz']}</td>
                    </tr>
                """
            
            html += f"""
                </table>
                <button class="show-more-btn" onclick="toggleRows('{match_id}', this)">Zobrazit vše ({len(vysledky_ev)})</button>
            </div>
            """

    html += """
        </div>
        <script>
            function toggleRows(matchId, btn) {
                const table = document.getElementById('table_' + matchId);
                const hiddenRows = table.querySelectorAll('.hidden-row');
                
                if (btn.innerText.includes('Zobrazit vše')) {
                    hiddenRows.forEach(row => row.style.display = 'table-row');
                    btn.innerText = 'Zobrazit méně';
                } else {
                    hiddenRows.forEach(row => row.style.display = 'none');
                    const count = table.querySelectorAll('tr').length - 1;
                    btn.innerText = 'Zobrazit vše (' + count + ')';
                }
            }
        </script>
    </body>
    </html>
    """
    
    with open(vystup, "w", encoding="utf-8") as f:
        f.write(html)


# --- HLAVNÍ SPOUŠTĚCÍ FUNKCE ---

def main():
    cookie = nacti_cookie()
    session = vytvor_session(cookie)
    
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "cs-CZ,cs;q=0.9",
        "Content-Type": "application/json;charset=utf-8",
        "Origin": "https://www.tipsport.cz",
        "Referer": "https://www.tipsport.cz/",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15",
    }

    print("Načítám hlavní stránku...")
    session.get("https://www.tipsport.cz/", headers=headers, timeout=15)
    time.sleep(1.5)

    print("Stahuji seznam zápasů pro Tipovačku...")
    zapasy = stahni_seznam_zapasu(session, headers)
    print(f"Nalezeno {len(zapasy)} zápasů.")

    vsechna_data = []

    for i, zapas in enumerate(zapasy):
        zid = zapas.get("id")
        nazev = zapas.get("nameFull", "?")
        datum = zapas.get("dateClosed", "")

        kurz_1 = kurz_x = kurz_2 = None
        if zapas.get("oppRows"):
            for k in zapas["oppRows"][0].get("oppsTab", []):
                if k is None: continue
                if k.get("label") == "1": kurz_1 = k.get("odd")
                elif k.get("label") == "0": kurz_x = k.get("odd")
                elif k.get("label") == "2": kurz_2 = k.get("odd")

        print(f"  [{i+1}/{len(zapasy)}] {nazev} - stahuji a aplikuji Shinovu metodu...")
        
        detail_raw = stahni_detail_zapasu(session, headers, zid)
        detail = parsuj_detail(detail_raw)

        vsechna_data.append({
            "id": zid,
            "nazev": nazev,
            "datum": datum,
            "kurz_1": kurz_1,
            "kurz_x": kurz_x,
            "kurz_2": kurz_2,
            **detail
        })

        time.sleep(1.5)

    output_json = "data.json"
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(vsechna_data, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Data úspěšně uložena do '{output_json}'")

    vygeneruj_html()
    print("🎉 Hotovo! Web se nahrává na GitHub Pages.")

if __name__ == "__main__":
    main()